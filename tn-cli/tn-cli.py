"""The Python implementation of the gRPC Tinode client."""

# To make print() compatible between p2 and p3
from __future__ import print_function

import argparse
import base64
import grpc
import json
import pkg_resources
import platform
try:
    import Queue as queue
except ImportError:
    import queue
import random
import shlex
import sys
import threading
import time

from google.protobuf import json_format

# Import generated grpc modules
from tinode_grpc import pb
from tinode_grpc import pbx

APP_NAME = "tn-cli"
APP_VERSION = "1.0.0"
LIB_VERSION = pkg_resources.get_distribution("tinode_grpc").version

# Dictionary wich contains lambdas to be executed when server response is received
onCompletion = {}

# Saved topic: default topic name to make keyboard input easier
SavedTopic = None

# IO queues and thread for asynchronous input/output
input_queue = queue.Queue()
output_queue = queue.Queue()
input_thread = None

# Default values for user and topic
default_user = None
default_topic = None

# Pack user's name and avatar into a vcard represented as json.
def make_vcard(fn, photofile):
    card = None

    if (fn != None and fn.strip() != "") or photofile != None:
        card = {}
        if fn != None:
            card['fn'] = fn.strip()

        if photofile != None:
            try:
                f = open(photofile, 'rb')
                # File extension is used as a file type
                # TODO: use mimetype.guess_type(ext) instead
                card['photo'] = {'data': base64.b64encode(f.read()), 'type': os.path.splitext(photofile)[1]}
            except IOError as err:
                stdoutln("Error opening '" + photofile + "'", err)

    return card

def parse_cred(cred):
    result = None
    if cred != None:
        result = []
        for c in cred.split(","):
            parts = c.split(":")
            result.append(pb.Credential(method=parts[0], value=parts[1]))

    return result

# Support for asynchronous input-output to/from stdin/stdout
def stdout(*args):
    text = ""
    for a in args:
        text = text + str(a) + " "
    text = text.strip(" ")
    if text != "":
        output_queue.put(text)

def stdoutln(*args):
    args = args + ("\n",)
    stdout(*args)

def stdin(input_queue):
    while True:
        cmd = sys.stdin.readline().splitlines()[0]
        input_queue.put(cmd)
        if cmd == 'exit' or cmd == 'quit':
            return

def encode_to_bytes(src):
    if src == None:
        return None
    return json.dumps(src).encode('utf-8')

# Constructing individual messages
def hiMsg(id):
    onCompletion[str(id)] = lambda params: print_server_params(params)
    return pb.ClientMsg(hi=pb.ClientHi(id=str(id), user_agent=APP_NAME + "/" + APP_VERSION + " (" +
        platform.system() + "/" + platform.release() + "); gRPC-python/" + LIB_VERSION,
        ver=LIB_VERSION, lang="EN"))

def accMsg(id, user, scheme, secret, uname, password, do_login, fn, photo, private, auth, anon, tags, cred):
    if secret == None and uname != None:
        if password == None:
            password = ''
        secret = str(uname) + ":" + str(password)
    if secret:
        secret = secret.encode('utf-8')
    else:
        secret = b''
    print(default_user)
    public = encode_to_bytes(make_vcard(fn, photo)) if (fn or photo) else None
    private = encode_to_bytes(private) if private else None
    return pb.ClientMsg(acc=pb.ClientAcc(id=str(id), user_id=user,
        scheme=scheme, secret=secret, login=do_login, tags=tags.split(",") if tags else None,
        desc=pb.SetDesc(default_acs=pb.DefaultAcsMode(auth=auth, anon=anon),
        public=public, private=private), cred=parse_cred(cred)), on_behalf_of=default_user)

def loginMsg(id, scheme, secret, cred, uname, password):
    if secret == None:
        if uname == None:
            uname = ''
        if password == None:
            password = ''
        secret = str(uname) + ":" + str(password)
        secret = secret.encode('utf-8')
    else:
        # Assuming secret is a base64-encoded string
        secret = base64.b64decode(secret)

    onCompletion[str(id)] = lambda params: save_cookie(params)
    return pb.ClientMsg(login=pb.ClientLogin(id=str(id), scheme=scheme, secret=secret,
        cred=parse_cred(cred)))

def subMsg(id, topic, fn, photo, private, auth, anon, mode, tags, get_query):
    if not topic:
        topic = default_topic
    if get_query:
        get_query = pb.GetQuery(what=get_query.split(",").join(" "))
    public = encode_to_bytes(make_vcard(fn, photo))
    private = encode_to_bytes(private)
    return pb.ClientMsg(sub=pb.ClientSub(id=str(id), topic=topic,
        set_query=pb.SetQuery(
            desc=pb.SetDesc(public=public, private=private, default_acs=pb.DefaultAcsMode(auth=auth, anon=anon)),
            sub=pb.SetSub(mode=mode),
            tags=tags.split(",") if tags else None), get_query=get_query), on_behalf_of=default_user)

def leaveMsg(id, topic, unsub):
    if not topic:
        topic = default_topic
    return pb.ClientMsg(leave=pb.ClientLeave(id=str(id), topic=topic, unsub=unsub), on_behalf_of=default_user)

def pubMsg(id, topic, content):
    if not topic:
        topic = default_topic
    return pb.ClientMsg(pub=pb.ClientPub(id=str(id), topic=topic, no_echo=True,
                content=encode_to_bytes(content)), on_behalf_of=default_user)

def getMsg(id, topic, desc, sub, tags, data):
    if not topic:
        topic = default_topic

    what = []
    if desc:
        what.append("desc")
    if sub:
        what.append("sub")
    if tags:
        what.append("tags")
    if data:
        what.append("data")
    return pb.ClientMsg(get=pb.ClientGet(id=str(id), topic=topic,
        query=pb.GetQuery(what=" ".join(what))), on_behalf_of=default_user)


def setMsg(id, topic, user, fn, photo, public, private, auth, anon, mode, tags):
    if not topic:
        topic = default_topic

    if public == None:
        public = encode_to_bytes(make_vcard(fn, photo))
    else:
        public = encode_to_bytes(public)
    private = encode_to_bytes(private)
    return pb.ClientMsg(set=pb.ClientSet(id=str(id), topic=topic,
        query=pb.SetQuery(
            desc=pb.SetDesc(default_acs=pb.DefaultAcsMode(auth=auth, anon=anon),
                public=public, private=private),
        sub=pb.SetSub(user_id=user, mode=mode),
        tags=tags)), on_behalf_of=default_user)


def delMsg(id, topic, what, param, hard):
    if topic == None and param != None:
        topic = param
        param = None

    if not topic:
        topic = default_topic

    stdoutln(id, topic, what, param, hard)
    enum_what = None
    before = None
    seq_list = None
    user = None
    if what == 'msg':
        enum_what = pb.ClientDel.MSG
        if param == 'all':
            seq_list = [pb.DelQuery(range=pb.SeqRange(low=1, hi=0x8FFFFFF))]
        elif param != None:
            seq_list = [pb.DelQuery(seq_id=int(x.strip())) for x in param.split(',')]
        stdoutln(seq_list)

    elif what == 'sub':
        enum_what = pb.ClientDel.SUB
        user = param
    elif what == 'topic':
        enum_what = pb.ClientDel.TOPIC

    # Field named 'del' conflicts with the keyword 'del. This is a work around.
    msg = pb.ClientMsg(on_behalf_of=default_user)
    xdel = getattr(msg, 'del')
    """
    setattr(msg, 'del', pb.ClientDel(id=str(id), topic=topic, what=enum_what, hard=hard,
        del_seq=seq_list, user_id=user))
    """
    xdel.id = str(id)
    xdel.topic = topic
    xdel.what = enum_what
    if hard != None:
        xdel.hard = hard
    if seq_list != None:
        xdel.del_seq.extend(seq_list)
    if user != None:
        xdel.user_id = user
    return msg

def noteMsg(id, topic, what, seq):
    if not topic:
        topic = default_topic

    enum_what = None
    if what == 'kp':
        enum_what = pb.KP
        seq = None
    elif what == 'read':
        enum_what = pb.READ
        seq = int(seq)
    elif what == 'recv':
        enum_what = pb.READ
        seq = int(seq)
    return pb.ClientMsg(note=pb.ClientNote(topic=topic, what=enum_what, seq_id=seq), on_behalf_of=default_user)

def parse_cmd(cmd):
    """Parses command line input into a dictionary"""
    parts = shlex.split(cmd)
    if len(parts) == 0:
        return None

    parser = None
    if parts[0] == ".use":
        parser = argparse.ArgumentParser(prog=parts[0], description='Set default user or topic')
        parser.add_argument('--user', default="unchanged", help='ID of the default user')
        parser.add_argument('--topic', default="unchanged", help='Name of default topic')
    elif parts[0] == "acc":
        parser = argparse.ArgumentParser(prog=parts[0], description='Create or alter an account')
        parser.add_argument('--user', default='new', help='ID of the account to update')
        parser.add_argument('--scheme', default='basic', help='authentication scheme, default=basic')
        parser.add_argument('--secret', default=None, help='secret for authentication')
        parser.add_argument('--uname', default=None, help='user name for basic authentication')
        parser.add_argument('--password', default=None, help='password for basic authentication')
        parser.add_argument('--do-login', action='store_true', help='login with the newly created account')
        parser.add_argument('--tags', action=None, help='tags for user discovery, comma separated list without spaces')
        parser.add_argument('--fn', default=None, help='user\'s human name')
        parser.add_argument('--photo', default=None, help='avatar file name')
        parser.add_argument('--private', default=None, help='user\'s private info')
        parser.add_argument('--auth', default=None, help='default access mode for authenticated users')
        parser.add_argument('--anon', default=None, help='default access mode for anonymous users')
        parser.add_argument('--cred', default=None, help='credentials, comma separated list in method:value format, e.g. email:test@example.com,tel:12345')
    elif parts[0] == "login":
        parser = argparse.ArgumentParser(prog=parts[0], description='Authenticate current session')
        parser.add_argument('--scheme', default='basic', help='authentication schema, default=basic')
        parser.add_argument('secret', nargs='?', default=argparse.SUPPRESS, help='authentication secret')
        parser.add_argument('--secret', dest='secret', default=None, help='authentication secret')
        parser.add_argument('--uname', default=None, help='user name in basic authentication scheme')
        parser.add_argument('--password', default=None, help='password in basic authentication scheme')
        parser.add_argument('--cred', default=None, help='credentials, comma separated list in method:value format, e.g. email:test@example.com,tel:12345')
    elif parts[0] == "sub":
        parser = argparse.ArgumentParser(prog=parts[0], description='Subscribe to topic')
        parser.add_argument('topic', nargs='?', default=argparse.SUPPRESS, help='topic to subscribe to')
        parser.add_argument('--topic', dest='topic', default=None, help='topic to subscribe to')
        parser.add_argument('--fn', default=None, help='topic\'s user-visible name')
        parser.add_argument('--photo', default=None, help='avatar file name')
        parser.add_argument('--private', default=None, help='topic\'s private info')
        parser.add_argument('--auth', default=None, help='default access mode for authenticated users')
        parser.add_argument('--anon', default=None, help='default access mode for anonymous users')
        parser.add_argument('--mode', default=None, help='new value of access mode')
        parser.add_argument('--tags', default=None, help='tags for topic discovery, comma separated list without spaces')
        parser.add_argument('--get-query', default=None, help='query for topic metadata or messages, comma separated list without spaces')
    elif parts[0] == "leave":
        parser = argparse.ArgumentParser(prog=parts[0], description='Detach or unsubscribe from topic')
        parser.add_argument('topic', nargs='?', default=argparse.SUPPRESS, help='topic to detach from')
        parser.add_argument('--topic', dest='topic', default=None, help='topic to detach from')
        parser.add_argument('--unsub', action='store_true', help='detach and unsubscribe from topic')
    elif parts[0] == "pub":
        parser = argparse.ArgumentParser(prog=parts[0], description='Send message to topic')
        parser.add_argument('topic', nargs='?', default=argparse.SUPPRESS, help='topic to publish to')
        parser.add_argument('--topic', dest='topic', default=None, help='topic to publish to')
        parser.add_argument('content', nargs='?', default=argparse.SUPPRESS, help='message to send')
        parser.add_argument('--content', dest='content', help='message to send')
    elif parts[0] == "get":
        parser = argparse.ArgumentParser(prog=parts[0], description='Query topic for messages or metadata')
        parser.add_argument('topic', nargs='?', default=argparse.SUPPRESS, help='topic to update')
        parser.add_argument('--topic', dest='topic', default=None, help='topic to update')
        parser.add_argument('--desc', action='store_true', help='query topic description')
        parser.add_argument('--sub', action='store_true', help='query topic subscriptions')
        parser.add_argument('--tags', action='store_true', help='query topic tags')
        parser.add_argument('--data', action='store_true', help='query topic messages')
    elif parts[0] == "set":
        parser = argparse.ArgumentParser(prog=parts[0], description='Update topic metadata')
        parser.add_argument('topic', help='topic to update')
        parser.add_argument('--fn', default=None, help='topic\'s name')
        parser.add_argument('--photo', default=None, help='avatar file name')
        parser.add_argument('--public', default=None, help='topic\'s public info, alternative to fn+photo')
        parser.add_argument('--private', default=None, help='topic\'s private info')
        parser.add_argument('--auth', default=None, help='default access mode for authenticated users')
        parser.add_argument('--anon', default=None, help='default access mode for anonymous users')
        parser.add_argument('--user', default=None, help='ID of the account to update')
        parser.add_argument('--mode', default=None, help='new value of access mode')
        parser.add_argument('--tags', default=None, help='tags for topic discovery, comma separated list without spaces')
    elif parts[0] == "del":
        parser = argparse.ArgumentParser(prog=parts[0], description='Delete message(s), subscription or topic')
        parser.add_argument('topic', nargs='?', default=argparse.SUPPRESS, help='topic being affected')
        parser.add_argument('--topic', dest='topic', default=None, help='topic being affected')
        parser.add_argument('what', default='msg', choices=('msg', 'sub', 'topic'),
            help='what to delete')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--user', dest='param', help='delete subscription with the given user id')
        group.add_argument('--list', dest='param', help='comma separated list of message IDs to delete')
        parser.add_argument('--hard', action='store_true', help='hard-delete messages')
    elif parts[0] == "note":
        parser = argparse.ArgumentParser(prog=parts[0], description='Send notification to topic, ex "note kp"')
        parser.add_argument('topic', help='topic to notify')
        parser.add_argument('what', nargs='?', default='kp', const='kp', choices=['kp', 'read', 'recv'],
            help='notification type')
        parser.add_argument('--seq', help='value being reported')
    else:
        print("Unrecognized:", parts[0])
        print("Possible commands:")
        print("\t.use\t- set default user or topic")
        print("\tacc\t- create account")
        print("\tlogin\t- authenticate")
        print("\tsub\t- subscribe to topic")
        print("\tleave\t- detach or unsubscribe from topic")
        print("\tpub\t- post message to topic")
        print("\tget\t- query topic for metadata or messages")
        print("\tset\t- update topic metadata")
        print("\tdel\t- delete message(s), topic or subscription")
        print("\tnote\t- send notification")
        print("\n\tType <command> -h for help")
        return None

    try:
        args = parser.parse_args(parts[1:])
        args.cmd = parts[0]
        return args
    except SystemExit:
        return None

def serialize_cmd(string, id):
    """Take string read from the command line, convert in into a protobuf message"""

    # Convert string into a dictionary
    cmd = parse_cmd(string)
    if cmd == None:
        return None

    # Process dictionary
    if cmd.cmd == ".use":
        if cmd.user != "unchanged":
            global default_user
            default_user = cmd.user
            stdoutln("Default user is '" + default_user + "'")
        if cmd.topic != "unchanged":
            global default_topic
            default_topic = cmd.topic
            stdoutln("Default topic is '" + default_topic + "'")
        return None
    elif cmd.cmd == "acc":
        return accMsg(id, cmd.user, cmd.scheme, cmd.secret, cmd.uname, cmd.password,
            cmd.do_login, cmd.fn, cmd.photo, cmd.private, cmd.auth, cmd.anon, cmd.tags, cmd.cred)
    elif cmd.cmd == "login":
        return loginMsg(id, cmd.scheme, cmd.secret, cmd.cred, cmd.uname, cmd.password)
    elif cmd.cmd == "sub":
        return subMsg(id, cmd.topic, cmd.fn, cmd.photo, cmd.private, cmd.auth, cmd.anon,
            cmd.mode, cmd.tags, cmd.get_query)
    elif cmd.cmd == "leave":
        return leaveMsg(id, cmd.topic, cmd.unsub)
    elif cmd.cmd == "pub":
        return pubMsg(id, cmd.topic, cmd.content)
    elif cmd.cmd == "get":
        return getMsg(id, cmd.topic, cmd.desc, cmd.sub, cmd.tags, cmd.data)
    elif cmd.cmd == "set":
        return setMsg(id, cmd.topic, cmd.user, cmd.fn, cmd.photo, cmd.public, cmd.private,
            cmd.auth, cmd.anon, cmd.mode, cmd.tags)
    elif cmd.cmd == "del":
        return delMsg(id, cmd.topic, cmd.what, cmd.param, cmd.hard)
    elif cmd.cmd == "note":
        return noteMsg(id, cmd.topic, cmd.what, cmd.seq)
    else:
        stdoutln("Unrecognized: " + cmd.cmd)
        return None

def gen_message(schema, secret):
    """Client message generator: reads user input as string,
    converts to pb.ClientMsg, and yields"""
    global input_thread

    random.seed()
    id = random.randint(10000,60000)

    # Asynchronous input-output
    input_thread = threading.Thread(target=stdin, args=(input_queue,))
    input_thread.daemon = True
    input_thread.start()

    yield hiMsg(id)

    if schema != None:
        id += 1
        yield loginMsg(id, schema, secret, None, None, None)

    print_prompt = True

    while True:
        if not input_queue.empty():
            id += 1
            inp = input_queue.get()
            if inp == 'exit' or inp == 'quit':
                return
            cmd = serialize_cmd(inp, id)
            print_prompt = True
            if cmd != None:
                yield cmd

        elif not output_queue.empty():
            sys.stdout.write("\r"+output_queue.get())
            sys.stdout.flush()
            print_prompt = True

        else:
            if print_prompt:
                sys.stdout.write("tn-cli> ")
                sys.stdout.flush()
                print_prompt = False
            time.sleep(0.1)

def run(addr, schema, secret):
    try:
        channel = grpc.insecure_channel(addr)
        stub = pbx.NodeStub(channel)
        # Call the server
        stream = stub.MessageLoop(gen_message(schema, secret))

        # Read server responses
        for msg in stream:
            if msg.HasField("ctrl"):
                # Run code on command completion
                func = onCompletion.get(msg.ctrl.id)
                if func != None:
                    del onCompletion[msg.ctrl.id]
                    if msg.ctrl.code >= 200 and msg.ctrl.code < 400:
                        func(msg.ctrl.params)
                stdoutln("\r" + str(msg.ctrl.code) + " " + msg.ctrl.text)
            elif msg.HasField("data"):
                stdoutln("\rFrom: " + msg.data.from_user_id + ":\n")
                stdoutln(json.loads(msg.data.content))
            elif msg.HasField("pres"):
                pass
            elif msg.HasField("info"):
                user = getattr(msg.info, 'from')
                stdoutln("\rMessage #" + str(msg.info.seq) + " " + msg.info.what +
                    " by " + user + "; topic=" + msg.info.topic + "(" + msg.topic + ")")
            else:
                stdoutln("\rMessage type not handled", msg)

    except grpc._channel._Rendezvous as err:
        print(err)
        channel.close()
        if input_thread != None:
            input_thread.join(0.3)

def read_cookie():
    try:
        cookie = open('.tn-cli-cookie', 'r')
        params = json.load(cookie)
        cookie.close()
        return params.get("token")

    except Exception as err:
        println("Missing or invalid cookie file '.tn-cli-cookie'", err)
        return None

def save_cookie(params):
    if params == None:
        return

    # Protobuf map 'params' is not a python object or dictionary. Convert it.
    nice = {}
    for p in params:
        nice[p] = json.loads(params[p])

    stdoutln("Authenticated as", nice.get('user'))

    try:
        cookie = open('.tn-cli-cookie', 'w')
        json.dump(nice, cookie)
        cookie.close()
    except Exception as err:
        stdoutln("Failed to save authentication cookie", err)

def print_server_params(params):
    stdoutln("\rConnected to server:")
    for p in params:
         stdoutln("\t" + p + ": " + json.loads(params[p]))

if __name__ == '__main__':
    """Parse command-line arguments. Extract host name and authentication scheme, if one is provided"""
    purpose = "Tinode command line client. Version " + APP_VERSION + "/" + LIB_VERSION + "."
    print(purpose)
    parser = argparse.ArgumentParser(description=purpose)
    parser.add_argument('--host', default='localhost:6061', help='address of Tinode server')
    parser.add_argument('--login-basic', help='login using basic authentication username:password')
    parser.add_argument('--login-token', help='login using token authentication')
    parser.add_argument('--login-cookie', action='store_true', help='read token from cookie file and use it for authentication')
    parser.add_argument('--no-login', action='store_true', help='do not login even if cookie file is present')
    args = parser.parse_args()

    stdoutln("Server '" + args.host + "'")

    schema = None
    secret = None

    if not args.no_login:
        if args.login_token:
            """Use token to login"""
            schema = 'token'
            secret = args.login_token.encode('acsii')
            print("Logging in with token", args.login_token)

        elif args.login_basic:
            """Use username:password"""
            schema = 'basic'
            secret = base64.b64encode(args.login_basic.encode('utf-8'))
            print("Logging in with login:password", args.login_basic)

        else:
            """Try reading the cookie file"""
            try:
                schema = 'token'
                secret = read_cookie()
                print("Logging in with cookie file")
            except Exception as err:
                print("Failed to read authentication cookie", err)

    run(args.host, schema, secret)
