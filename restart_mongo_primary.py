#!/tmp/venv/bin/python
import sys
import os
import time
import socket
import pymongo
from traceback import print_exc
from pymongo.mongo_client import MongoClient
from subprocess import Popen, PIPE, STDOUT
from pymongo.errors import ConnectionFailure

def getMyConfig(config, me):
    for i in range(len(config['members'])):
       if config['members'][i]['host'] == me:
           myConfig = config['members'][i]
           break
    return myConfig

def isSecondary(db):
    status = db.command("replSetGetStatus")
    retVal = False
    if status['myState'] != 2:
        retVal = False
    else:
        retVal = True
    return retVal

def main():
    if len(sys.argv) != 3:
        raise Exception("invalid args")
    try:
        rs_name  = sys.argv[1]
        rs_port  = sys.argv[2]
        client   = MongoClient(host="localhost", port=int(rs_port))

        try:
            client.admin.command('ismaster')
        except ConnectionFailure:
            print("Server not available")

        db       = client['admin']
        status   = db.command("replSetGetStatus")
        master   = db.command("isMaster")
        me       = master['me']
        set      = status['set']
        db       = client['local']
        myConfig = getMyConfig(db['system']['replset'].find_one({}), me)

    except Exception as e:
        raise Exception(e)

    if set != rs_name:
        raise Exception(str(set) is not str(rs_name))

    if not (status['myState'] == 1 and myConfig['priority'] == 100):
        msg = "skipping: node is either not a primary "
        msg += "or is not configured as a primary"
        print(msg)
        sys.exit(0)

    db     = client['admin']

    try:
        db.command({"replSetStepDown":120})
    except pymongo.errors.AutoReconnect as e:
        pass
    except Exception as e:
        raise(e)

    time.sleep(1)

    try:
        client = MongoClient(host="localhost", port=int(rs_port))
        try:
            client.admin.command('ismaster')
        except ConnectionFailure:
            print("Server not available")

    except Exception as e:
        raise(e)

    db = client['admin']
    print("waiting for step-down of primary to complete...")

    while not isSecondary(db):
        print("waiting for step-down of primary to complete...")
        time.sleep(1)

    print("host has become secondary; continuing...")
    print("sleeping for 30 seconds...")
    time.sleep(30)

    cmd_list = [
        'sudo /etc/init.d/mongodb-' + rs_name + ' stop',
        'sudo /etc/init.d/mongodb-' + rs_name + ' start'
    ]

    for cmd in cmd_list:
        p = Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=STDOUT, close_fds=True)
        output = p.stdout.read()
        retcode = p.wait()
        if retcode != 0:
            raise Exception(output)

try:
    main()
except Exception as e:
    print_exc()
    sys.exit(1)
