#!/tmp/venv-restart/bin/python
import sys
import time
import socket
import pymongo
from traceback import print_exc
from pymongo.mongo_client import MongoClient
from subprocess import Popen
from pymongo.errors import ConnectionFailure

def getMyConfig(config, me):
    for i in range(len(config['members'])):
       if config['members'][i]['host'] == me:
           myConfig = config['members'][i]
           break
    return myConfig

def main():
    if len(sys.argv) != 4:
        raise Exception("invalid args")
    
    try:
        rs_name  = sys.argv[1]
        rs_port  = sys.argv[2]
        rs_prior = int(sys.argv[3])
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

    if status['myState'] != 2 or myConfig['priority'] == 100 or myConfig['priority'] != rs_prior:
        msg = "skipping: node is either not a secondary, "
        msg += "or is configured as a primary"
        print(msg)
        sys.exit(0)

    p = Popen(["sudo", "/etc/init.d/mongodb-"+rs_name, "stop"])
    retcode = p.wait()    
    msg = "stop() return code : " + str(retcode)
    msg += "; stderr: " + str(p.communicate()[1])
    print(msg)

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
    
    
    
