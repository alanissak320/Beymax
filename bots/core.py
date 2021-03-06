from .utils import load_db, save_db, Database, getname, validate_permissions, Interpolator
from .args import Arg, Argspec, UserType
import discord
from discord.compat import create_task
import asyncio
import time
import os
import yaml
import sys
import threading
import shlex
from functools import wraps
import re

mention_pattern = re.compile(r'<@.*?(\d+)>')

class CoreBot(discord.Client):
    nt = 0
    configuration = {}
    primary_server = None
    channel_references = {} # reference name -> channel name/id
    event_listeners = {} # event name -> [listener functions (self, event)]
    # changed to set in favor of event API
    commands = {} # !cmd -> docstring. Functions take (self, message, content)
    ignored_users = set()
    users = {} # id/fullname -> {id, fullname, mention, name}
    tasks = {} # taskname (auto generated) -> [interval(s), qualname] functions take (self)
    special = {} # eventname -> checker. callable takes (self, message) and returns True if function should be run. Func takes (self, message, content)
    special_order = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if os.path.exists('config.yml'):
            with open('config.yml') as reader:
                self.configuration = yaml.load(reader)
            self.command_prefix = self.config_get('prefix', default='!')

    def add_command(self, command, *spec, aliases=None, delimiter=None, empty=False, **kwargs): #decorator. Attaches the decorated function to the given command(s)
        if aliases is None:
            aliases = []
        for arg in spec:
            if isinstance(arg, str):
                raise TypeError("Please define command aliases using the aliases keyword")
        if self.config_get('use_shlex') and delimiter is not None:
            print(
                "Warning: (%s) The use of delimiters is discouraged in shlex mode. Instead, "
                "have users quote their arguments" % command
            )
        def wrapper(func):
            @wraps(func)
            async def on_cmd(self, cmd, message, content):
                if self.check_permissions_chain(self.strip_prefix(cmd), message.author)[0]:
                    print("Command in channel", message.channel, "from", message.author, ":", content)
                    if len(spec) or empty:
                        argspec = Argspec(cmd, *spec, **kwargs)
                        if not self.config_get('use_shlex'):
                            delim = delimiter
                        elif delimiter is not None and delimiter not in message.content:
                            delim = None
                        elif self.config_get('disable_delimiters'):
                            print("Warning: Ignoring delimiter")
                            delim = None
                        else:
                            delim = delimiter
                        result, content = argspec(*content[1:], delimiter=delim)
                        if not result:
                            await self.send_message(
                                message.channel,
                                content
                            )
                            return
                    try:
                        await func(self, message, content)
                    except discord.DiscordException:
                        await self.send_message(
                            message.channel,
                            "I've encountered an error communicating with Discord."
                            " This may be a transient issue, but if it occurs again"
                            " you should submit a bug report: `$!bug <Discord Exception> %s`"
                            % (message.content.replace('`', ''))
                        )
                        raise
                    except:
                        await self.send_message(
                            message.channel,
                            "I encountered unexpected error while processing your"
                            " command. Please submit a bug report: `$!bug <Python Exception> %s`"
                            % (message.content.replace('`', ''))
                        )
                        raise
                    self.dispatch('command', cmd, message.author)
                else:
                    print("Denied", message.author, "using command", cmd, "in", message.channel)
                    await self.send_message(
                        message.channel,
                        "You do not have permissions to use this command\n" +
                        # Add additional message if this is a DM and they may actually
                        # have permissions for this command
                        (("If you have permissions granted to you by a role, "
                         "I cannot check those in private messages\n")
                         if isinstance(message.channel, discord.PrivateChannel) and
                         self.primary_server is None
                         else ""
                        ) +
                        "To check your permissions, use the `$!permissions` command"
                    )
            for cmd in [command] + aliases:
                if not cmd.startswith(self.command_prefix):
                    cmd = self.command_prefix + cmd
                on_cmd = self.subscribe(cmd)(on_cmd)
                self.commands[cmd] = func.__doc__
            return on_cmd

        return wrapper

    def add_task(self, interval): #decorator. Sets the decorated function to run on the specified interval
        def wrapper(func):
            taskname = 'task:'+func.__name__
            if taskname in self.tasks:
                raise NameError("This task already exists! Change the name of the task function")
            self.tasks[taskname] = (interval, func.__qualname__)

            @self.subscribe(taskname)
            async def run_task(self, task):
                await func(self)
                if 'tasks' not in self.update_times:
                    self.update_times['tasks'] = {}
                self.update_times['tasks'][taskname] = time.time()
                save_db(self.update_times, 'tasks.json')


            return run_task
        return wrapper

    def add_special(self, check): #decorator. Sets the decorated function to run whenever the check is true
        def wrapper(func):
            event = 'special:'+func.__name__
            if event in self.special:
                raise NameError("This special event already exists! Change the name of the special function")
            self.special[event] = check
            self.special_order.append(event)

            @self.subscribe(event)
            async def run_special(self, evt, message, content):
                await func(self, message, content)

            return run_special
        return wrapper

    def subscribe(self, event): # decorator. Sets the decorated function to run on events
        # event functions should take the event, followed by expected arguments
        def wrapper(func):
            if str(event) not in self.event_listeners:
                self.event_listeners[str(event)] = []
            self.event_listeners[str(event)].append(func)
            # func.unsubscribe will unsubscribe the function from the event
            # calling without args unsubscribes from the most recent event that this
            # function was subscribed to. An event can be specified to unsubscribe
            # from a specific event, if the function was subscribed to several
            func.unsubscribe = lambda x=str(event):self.event_listeners[x].remove(func)
            return func
        return wrapper

    def reserve_channel(self, name):
        # creates a channel reference by that name
        # channel references can be changed in configuration
        self.channel_references[name] = None

    def fetch_channel(self, name):
        channel = self.channel_references[name] if name in self.channel_references else None
        if channel is None:
            return self.fetch_channel('general')
        return channel

    def EnableAll(self, *bots): #convenience function to enable a bunch of subbots at once
        for bot in bots:
            if callable(bot):
                self = bot(self)
            else:
                raise TypeError("Bot is not callable")
        return self

    def strip_prefix(self, command):
        if command.startswith(self.command_prefix):
            return command[len(self.command_prefix):]
        return command

    def dispatch(self, event, *args, manual=False, **kwargs):
        self.nt += 1
        output = []
        if not manual:
            if 'before:'+str(event) in self.event_listeners:
                output += self.dispatch_event('before:'+str(event), *args, **kwargs)
            super().dispatch(event, *args, **kwargs)
            if str(event) in self.event_listeners:
                output += self.dispatch_event(str(event), *args, **kwargs)
            if 'after:'+str(event) in self.event_listeners:
                output += self.dispatch_event('after:'+str(event), *args, **kwargs)
        else:
            if str(event) in self.event_listeners:
                output += self.dispatch_event(str(event), *args, **kwargs)
        return output

    def dispatch_event(self, event, *args, **kwargs):
        return [
            create_task(listener(self, event, *args, **kwargs), loop=self.loop)
            for listener in self.event_listeners[event]
        ]



    def config_get(self, *keys, default=None):
        obj = self.configuration
        for key in keys:
            if key in obj:
                obj = obj[key]
            else:
                return default
        return obj

    async def on_ready(self):
        print("Connected to the following servers")
        if 'primary_server' in self.configuration:
            self.primary_server = discord.utils.get(
                self.servers,
                id=str(self.configuration['primary_server'])
            )
            if self.primary_server is None:
                sys.exit("Primary server set, but no matching server was found")
            else:
                print("Validated primary server:", self.primary_server.name)
        else:
            print("Warning: No primary server set in configuration. Role permissions cannot be validated in PM's")
        first = True
        for server in list(self.servers):
            print(server.name, server.id)
            await self.on_server_join(server)
        print("Commands:", [cmd for cmd in self.commands])
        print(
            "Tasks:",
            '\n'.join([
                '%s every %d seconds (Runs %s)' % (
                    taskname,
                    *self.tasks[taskname]
                ) for taskname in self.tasks
            ])
        )
        self._general = discord.utils.get(
            self.get_all_channels(),
            name='general',
            type=discord.ChannelType.text
        )
        self.update_times = load_db('tasks.json')
        taskkey = ''.join(sorted(self.tasks))
        if 'key' not in self.update_times or self.update_times['key'] != taskkey:
            print("Invalidating task time cache")
            self.update_times = {'key':taskkey, 'tasks':{}}
            save_db(self.update_times, 'tasks.json')
        else:
            print("Not invalidating cache")
        self.permissions = None
        self.channel_references['general'] = self._general
        if 'channels' in self.configuration:
            for name in self.channel_references:
                if name in self.configuration['channels']:
                    channel = discord.utils.get(
                        self.get_all_channels(),
                        name=self.configuration['channels'][name],
                        type=discord.ChannelType.text
                    )
                    if channel is None:
                        channel = discord.utils.get(
                            self.get_all_channels(),
                            id=self.configuration['channels'][name],
                            type=discord.ChannelType.text
                        )
                    if channel is None:
                        raise NameError("No channel by name of "+self.configuration['channels'][name])
                    self.channel_references[name] = channel
                else:
                    print("Warning: Channel reference", name, "is not defined")
        print(self.channel_references)
        self.ignored_users = set(load_db('ignores.json', []))
        if os.path.exists('permissions.yml'):
            with open('permissions.yml') as reader:
                self.permissions = yaml.load(reader)
            #get user by name: server.get_member_named
            #get user by id: server.get_member
            #iterate over server.role_hierarchy until the command is found (default enabled)
            #validate the permissions object
            if not isinstance(self.permissions, dict):
                sys.exit("permissions.yml must be a dictionary")
            if 'defaults' not in self.permissions:
                sys.exit("permissions.yml must define defaults")
            validate_permissions(self.permissions['defaults'], True)
            if 'permissions' in self.permissions:
                if not isinstance(self.permissions['permissions'], list):
                    sys.exit("permissions key of permissions.yml must be a list")
            seen_roles = set()
            for target in self.permissions['permissions']:
                validate_permissions(target)
                if 'role' in target:
                    if target['role'] in seen_roles:
                        sys.exit("Duplicate role encountered in permissions.yml")
                    seen_roles.add(target['role'])
            self.permissions['roles'] = {
                discord.utils.find(
                    lambda role: role.name == obj['role'] or role.id == obj['role'],
                    [_role for server in self.servers for _role in server.roles]
                ).id:obj for obj in self.permissions['permissions']
                if 'role' in obj
            }
            try:
                tmp = [
                    (self.getid(user),obj) for obj in self.permissions['permissions']
                    if 'users' in obj
                    for user in obj['users']
                ]
            except NameError as e:
                raise SystemExit("Unable to find user") from e
            self.permissions['users'] = {}
            for uid, rule in tmp:
                if uid not in self.permissions['users']:
                    self.permissions['users'][uid] = [rule]
                else:
                    self.permissions['users'][uid].append(rule)
            for uid in self.permissions['users']:
                self.permissions['users'][uid].sort(
                    key=lambda x:len(x['users'])
                )
            self.permissions['defaults']['_grant'] = 'by default'
            for user in self.permissions['users']:
                for i in range(len(self.permissions['users'][user])):
                    nUsers = len(self.permissions['users'][user][i]['users'])
                    self.permissions['users'][user][i]['_grant'] = (
                        'directly to you' if nUsers == 1 else
                        'to you and %d other people' % nUsers
                    )
            for role in self.permissions['roles']:
                self.permissions['roles'][role]['_grant'] = 'by role `%s`' % (
                    self.permissions['roles'][role]['role']
                )
        self.task_worker = threading.Thread(
            target=CoreBot._run_tasks,
            args=(self,),
            daemon=True,
            name="CoreBot Background Task Thread"
        )

        self.task_worker.start()

    async def shutdown(self):
        tasks = self.dispatch('cleanup')
        if len(tasks):
            print("Waiting for ", len(tasks), "cleanup tasks to complete")
            await asyncio.wait(tasks)
        await self.close()

    async def send_message(self, destination, content, *, delim='\n', quote='', interp=None, **kwargs):
        #built in chunking
        if interp is None:
            interp = Interpolator(self, destination)
        elif interp is False:
            interp = {}
        elif isinstance(interp, Interpolator):
            tmp = Interpolator(self, destination)
            tmp.update(**interp)
            interp = tmp
        elif isinstance(interp, discord.Channel):
            interp = Interpolator(self, interp)
        elif not isinstance(interp, dict):
            raise TypeError("Cannot infer interpolation settings from an object of type "+type(interp))
        try:
            for key in interp:
                content = content.replace(key, interp[key])
        except:
            print("Interpolation Error: ", {**interp})
        for match in mention_pattern.finditer(content):
            uid = match.group(1)
            do_sub = isinstance(destination, discord.User) and destination.id != uid
            do_sub |= hasattr(destination, 'server') and self.get_user(uid, destination.server) is None
            do_sub |= hasattr(destination, 'recipients') and uid not in {user.id for user in destination.recipients}
            if do_sub:
                # have to replace the mention with a `@Username`
                user = self.get_user(uid)
                if user is not None:
                    content = content.replace(
                        match.group(0),
                        '`@%s#%s`' % (user.name, str(user.discriminator)),
                        1
                    )
        body = content.split(delim)
        tmp = []
        last_msg = None
        for line in body:
            tmp.append(line)
            msg = delim.join(tmp)
            if len(msg) > 2048 and delim=='. ':
                # If the message is > 2KB and we're trying to split by sentences,
                # try to split it up by spaces
                last_msg = await self.send_message(
                    destination,
                    msg,
                    delim=' ',
                    interp=False,
                    **kwargs
                )
            elif len(msg) > 1536 and delim=='\n':
                # if the message is > 1.5KB and we're trying to split by lines,
                # try to split by sentences
                last_msg = await self.send_message(
                    destination,
                    msg,
                    delim='. ',
                    interp=False,
                    **kwargs
                )
            elif len(msg) > 1024:
                # Otherwise, send it if the current message has reached the
                # 1KB chunking target
                try:
                    last_msg = await super().send_message(
                        destination,
                        quote+msg+quote,
                        **kwargs
                    )
                except discord.errors.HTTPException as e:
                    print("Failed to deliver message:", e.text)
                    await super().send_message(
                        self.fetch_channel('dev'),
                        "Failed to deliver a message to "+str(destination)
                        )
                tmp = []
                await asyncio.sleep(1)
        if len(tmp):
            #send any leftovers (guaranteed <2KB)
            try:
                last_msg = await super().send_message(
                    destination,
                    quote+msg+quote
                )
            except discord.errors.HTTPException as e:
                print("Failed to deliver message:", e.text)
                await super().send_message(
                    self.fetch_channel('dev'),
                    "Failed to deliver a message to "+str(destination)
                )
        return last_msg

    def get_user(self, reference, *servers):
        if not len(servers):
            servers = list(self.servers)
            if self.primary_server is not None:
                servers = [self.primary_server] + servers
                #it's okay that the primary_server is duplicated
                #But at least this gives it priority
        for server in servers:
            result = server.get_member(reference)
            if result is not None:
                return result
        for server in servers:
            result = server.get_member_named(reference)
            if result is not None:
                return result

    def getid(self, username):
        #Get the id of a user from an unknown reference (could be their username, fullname, or id)
        result = self.get_user(username)
        if result is not None:
            if result.id != username and '#' not in username:
                raise NameError("Username '%s' not valid, must containe #discriminator" % username)
            return result.id
        raise NameError("Unable to locate member '%s'. Must use a user ID, username, or username#discriminator" % username)

    def build_permissions_chain(self, user):
        # Assemble the chain of permissions rules for a given user
        chain = []
        if user.id in self.permissions['users']:
            chain += self.permissions['users'][user.id]
        if self.primary_server is not None:
            user = self.primary_server.get_member(user.id)
        if hasattr(user, 'roles') and hasattr(user, 'server'):
            user_roles = set(user.roles)
            for role in user.server.role_hierarchy:
                if role in user_roles and role.id in self.permissions['roles']:
                    chain.append(self.permissions['roles'][role.id])
        return [item for item in chain] + [self.permissions['defaults']]

    def has_underscore_permissions(self, user, chain=None):
        # Check the permissions chain for a user to see if they can use
        # Administrative (underscore) commands
        if chain is None:
            #build the chain, if it wasn't given as an argument
            chain = self.build_permissions_chain(user)
        for obj in chain:
            if 'underscore' in obj:
                return obj['underscore']

    def check_permissions_chain(self, cmd, user, chain=None):
        #Important note: cmd argument does not include the leading ! of a command
        # Permissions.yml file contains commands without prefix, and we check them
        # here without the prefix
        if chain is None:
            #build the chain, if it wasn't given as an argument
            chain = self.build_permissions_chain(user)
        for obj in chain:
            if 'allow' in obj and (cmd in obj['allow'] or '$all' in obj['allow']):
                return True, obj['_grant']
            elif 'deny' in obj and (cmd in obj['deny'] or '$all' in obj['deny']):
                return False, obj['_grant']
            elif cmd.startswith('_') and 'underscore' in obj:
                return obj['underscore'], obj['_grant']
        return (not cmd.startswith('_'), 'by default') #default behavior

    async def on_message(self, message):
        if message.author == self.user:
            return
        if self.get_user(message.author.id) is None:
            #User is not a member of any known server
            #silently ignore
            return
        # build the user struct and update the users object
        if self.config_get('use_shlex'):
            try:
                lex = shlex.shlex(message.content.strip(), posix=True)
                lex.whitespace_split = True
                content = list(lex)
                content[0] = content[0].lower()
            except:
                return
        else:
            try:
                content = message.content.strip().split()
                content[0] = content[0].lower()
            except:
                return
        if message.author.id in self.ignored_users:
            print("Ignoring message from", message.author,":", content)
        elif content[0] in self.commands: #if the first argument is a command
            # dispatch command event
            print("Dispatching command")
            self.dispatch(content[0], message, content)
        else:
            # If this was not a command, check if any of the special functions
            # would like to run on this message
            for event in self.special_order:
                if self.special[event](self, message):
                    print("Running special", event)
                    self.dispatch(event, message, content)
                    break

    def _run_tasks(self):
        while True:
            time.sleep(60)
            # Check if it is time to run any tasks
            #
            current = time.time()
            ran_task = False
            for task, (interval, qualname) in self.tasks.items():
                last = 0
                if 'tasks' in self.update_times and task in self.update_times['tasks']:
                    last = self.update_times['tasks'][task]
                if current - last > interval:
                    print("Running task", task, '(', qualname, ')')
                    self.dispatch(task)

    async def on_server_join(self, server):
        if self.primary_server is not None and self.primary_server != server:
            try:
                await self.send_message(
                    discord.utils.get(
                        server.channels,
                        name='general',
                        type=discord.ChannelType.text
                    ),
                    "Unfortunately, this instance of $NAME is not configured"
                    " to run on multiple servers. Please contact the owner"
                    " of this instance, or run your own instance of $NAME."
                    " Goodbye!"
                )
            except:
                pass
            await self.leave_server(server)
        elif len(self.servers) > 1:
            print("Warning: Joining to multiple servers is not supported behavior")

def EnableUtils(bot): #prolly move to it's own bot
    #add some core commands
    if not isinstance(bot, CoreBot):
        raise TypeError("This function must take a CoreBot")

    bot.reserve_channel('dev')

    @bot.add_command('_task', Arg('task', type='extra', help='task_name'))
    async def cmd_task(self, message, args):
        """
        `$!_task <task name>` : Manually runs the named task
        """
        key = ' '.join([args.task] + args.extra)
        if not key.startswith('task:'):
            key = 'task:'+key
        if key in self.tasks:
            print("Manually running task", key, '(', self.tasks[key][1], ')')
            self.dispatch(key)
        else:
            await self.send_message(
                message.channel,
                "No such task"
            )

    @bot.add_command('_nt', empty=True)
    async def cmd_nt(self, message, content):
        await self.send_message(
            message.channel,
            '%d events have been dispatched' % self.nt
        )

    @bot.add_command('output-dev', empty=True)
    async def cmd_dev(self, message, content):
        """
        `$!output-dev` : Any messages that would always go to general will go to testing grounds
        """
        self._channel_references = {k:v for k,v in self.channel_references.items()}
        self.channel_references = {k:self.fetch_channel('dev') for k in self.channel_references}
        await self.send_message(
            self.fetch_channel('dev'),
            "Development mode enabled. I will send any messages which are not replies to $CHANNEL",
        )

    @bot.add_command('output-prod', empty=True)
    async def cmd_prod(self, message, content):
        """
        `$!output-prod` : Restores normal message routing
        """
        self.channel_references = {k:v for k,v in self._channel_references.items()}
        await self.send_message(
            self.fetch_channel('dev'),
            "Production mode enabled. All messages will be directed normally"
        )

    #Not using argparse API as it does not preserve whitespace
    @bot.add_command('_announce')
    async def cmd_announce(self, message, content):
        """
        `$!_announce <message>` : Forces me to say the given message in general.
        Example: `$!_announce I am really cool`
        """
        await self.send_message(
            self.fetch_channel('general'),
            message.content.strip().replace(self.command_prefix+'_announce', '', 1)
        )

    @bot.add_command('permissions', empty=True)
    async def cmd_perms(self, message, content):
        """
        `$!permissions` : Gets a list of commands you have permissions to use
        """
        chain = self.build_permissions_chain(message.author)
        cmds = []
        for command in sorted(self.commands):
            (allow, rule) = self.check_permissions_chain(self.strip_prefix(command), message.author, chain)
            if allow:
                cmds.append((
                    command,
                    rule
                ))
        body = ["Here are the commands you have permissions to use:"]
        for cmd, rule in cmds:
            body.append('`%s` : Granted **%s**' % (
                cmd,
                rule
            ))
        if isinstance(message.channel, discord.PrivateChannel) and self.primary_server is None:
            body.append(
                "You may have additional permissions granted to you by a role"
                " but I cannot check those within a private chat. Try the"
                " `$!permissions` command in a server channel"
            )
        await self.send_message(
            message.author,
            '\n'.join(body)
        )

    @bot.add_command('ignore', Arg('user', type=UserType(bot, by_nick=False), help="Username or ID"))
    async def cmd_ignore(self, message, args):
        """
        `$!ignore <user id or user#tag>` : Ignore all commands by the given user
        until the next time I'm restarted
        Example: `$!ignore Username#1234` Ignores all commands from Username#1234
        """
        uid = args.user.id
        if uid in self.ignored_users:
            await self.send_message(
                message.channel,
                "This user is already ignored"
            )
            return
        self.ignored_users.add(uid)
        save_db(
            list(self.ignored_users),
            'ignores.json'
        )
        for server in self.servers:
            user = server.get_member(uid)
            if user is not None:
                general = self.fetch_channel('general')
                if general.server != server:
                    general = discord.utils.get(
                        server.channels,
                        name='general',
                        type=discord.ChannelType.text
                    )
                if self.config_get('ignore_role') != None:
                    blacklist_role = self.config_get('ignore_role')
                    for role in server.roles:
                        if role.id == blacklist_role or role.name == blacklist_role:
                            await self.add_roles(
                                user,
                                role
                            )
                try:
                    await self.send_message(
                        general,
                        "%s has asked me to ignore %s. %s can no longer issue any commands"
                        " until they have been `$!pardon`-ed" % (
                            str(message.author),
                            str(user),
                            getname(user)
                        )
                    )
                except:
                    pass
        await self.send_message(
            args.user,
            "I have been asked to ignore you by %s. Please contact them"
            " to petition this decision." % (str(message.author))
        )

    @bot.add_command('pardon', Arg('user', type=UserType(bot, by_nick=False), help="Username or ID"))
    async def cmd_pardon(self, message, args):
        """
        `$!pardon <user id or user#tag>` : Pardons the user and allows them to issue
        commands again.
        Example: `$!pardon Username#1234` pardons Username#1234
        """
        uid = args.user.id
        if uid not in self.ignored_users:
            await self.send_message(
                message.channel,
                "This user is not currently ignored"
            )
            return
        self.ignored_users.remove(uid)
        save_db(
            list(self.ignored_users),
            'ignores.json'
        )
        for server in self.servers:
            user = server.get_member(uid)
            if user is not None:
                general = self.fetch_channel('general')
                if general.server != server:
                    general = discord.utils.get(
                        server.channels,
                        name='general',
                        type=discord.ChannelType.text
                    )
                if self.config_get('ignore_role') != None:
                    blacklist_role = self.config_get('ignore_role')
                    for role in server.roles:
                        if role.id == blacklist_role or role.name == blacklist_role:
                            await self.remove_roles(
                                user,
                                role
                            )
                try:
                    await self.send_message(
                        general,
                        "%s has pardoned %s" % (
                            str(message.author),
                            str(user)
                        )
                    )
                except:
                    pass
        await self.send_message(
            args.user,
            "You have been pardoned by %s. I will resume responding to "
            "your commands." % (str(message.author))
        )

    @bot.add_command('idof', Arg('query', type='extra', help="Entity to search for"))
    async def cmd_idof(self, message, args):
        """
        `$!idof <entity>` : Gets a list of all known entities by that name
        Example: `$!idof general` would list all users, channels, and roles with that name
        """
        servers = [message.server] if message.server is not None else self.servers
        result = []
        query = ' '.join([args.query] + args.extra).lower()
        for server in servers:
            first = True
            if query in server.name.lower():
                if first:
                    first = False
                    result.append('From server `%s`' % server.name)
                result.append('Server `%s` : %s' % (server.name, server.id))
            for channel in server.channels:
                if query in channel.name.lower():
                    if first:
                        first = False
                        result.append('From server `%s`' % server.name)
                    result.append('Channel `%s` : %s' % (channel.name, channel.id))
            for role in server.roles:
                if query in role.name.lower():
                    if first:
                        first = False
                        result.append('From server `%s`' % server.name)
                    result.append('Role `%s` : %s' % (role.name, role.id))
            for member in server.members:
                if member.nick is not None and query in member.nick.lower():
                    if first:
                        first = False
                        result.append('From server `%s`' % server.name)
                    result.append('Member `%s` aka `%s` : %s' % (
                        str(member),
                        member.nick,
                        member.id
                    ))
                elif query in member.name.lower():
                    if first:
                        first = False
                        result.append('From server `%s`' % server.name)
                    result.append('Member `%s`: %s' % (
                        str(member),
                        member.id
                    ))
        if len(result):
            await self.send_message(
                message.channel,
                '\n'.join(result)
            )
        else:
            await self.send_message(
                message.channel,
                "I was unable to find any entities by that name"
            )


    return bot
