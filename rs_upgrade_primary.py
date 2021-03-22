#!/tmp/venv-restart/bin/python
import sys
import time
import socket
import pymongo
from traceback import print_exc
from pymongo.mongo_client import MongoClient
from subprocess import Popen
from pymongo.errors import ConnectionFailure

#
# usage: rs_upgrade_primary.py <rs_name> <port> 
#
# note: should probably modify this to be more 
# flexible in the future (i.e. primaries won't necessarily
# always be configured with a priority == 100 per se
# (see rs_upgrade_secondary.py as a reference)
#

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
    
    db = client['admin']
    
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
    
    p = Popen(["sudo", "/etc/init.d/mongodb-"+rs_name, "stop"])
    retcode = p.wait()    
    
    msg = "stop() return code : " + str(retcode)
    msg += "; stderr: " + str(p.communicate()[1])
    print(msg)

    print("sleeping for 30 seconds...")
    time.sleep(30)
    
    p = Popen(["sudo", "/etc/init.d/mongodb-"+rs_name, "start"])
    retcode = p.wait()
    
    msg = "start() return code : " + str(retcode)
    msg += "; stderr: " + str(p.communicate()[1])
    print(msg) 
    
    if retcode != 0:
        raise Exception(msg)

try: 
    main() 
except Exception as e: 
    print_exc() 
    sys.exit(1)

