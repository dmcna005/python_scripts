#!/usr/bin/env python
import os, sys, time, errno, datetime, traceback
import socket, smtplib, re, zipfile, shutil
import tarfile
from ConfigParser import RawConfigParser
from decryption import user, passwd
from stat import *
from time import gmtime, strftime
#from shutil import make_archive
#from cryptography.fernet import Fernet
from subprocess import call, Popen, PIPE, STDOUT, check_call, CalledProcessError
#from backup_conf import appdb, oplog1, oplog2, npoplog, backup_directory, retention_time
#from backup_conf import smtp_server, smtp_alert_recipient, opsmgr_dbs

__author__ = 'Dwayne McNab'
__version__ = '1.0.1'
__license__ = 'equifax'

# Do not make changes to this script. Instead use the backup.conf files
# to overwrite the following variables
config = RawConfigParser()
config.read('backup.conf')
appdb = config.get('APPDB', 'appdb')
oplog1 = config.get('OPLOG1', 'oplog1')
oplog2 = config.get('OPLOG2', 'oplog2')
npoplog = config.get('NPOPLOG', 'npoplog')
backup_directory = config.get('DEFAULT', 'backup_directory')
retention_time = int(config.get('DEFAULT', 'retention_time'))
smtp_server = conf.get('DEFAULT', 'smtp_server')
smtp_alert_recipient = conf.get('DEFAULT', 'smtp_alert_recipient')
opsmgr_dbs = [appdb, oplog1, oplog2, npoplog]

# Because I want the name to be shorter :)
backup_dir = backup_directory

if not os.path.exists(backup_dir):
    raise OSError('backup directory does not exist! exiting script')
    send = sendMailAlert('backup directory %s was removed!!' % backup_dir)
    exit(1)

    try:
        os.makedirs(backup_dir)
    except OSError as e:
        if e.errno == errno.EXIST:
            os.chdir(backup_dir)

now = time.time()
retention = (retention_time * 86400)
old_time = (now - retention)
#old_time = datetime.datetime.now() - datetime.timedelta(minutes=15)
mod_time = strftime("%a-%d-%b-%Y-%H-%M-%S", gmtime())
archives = os.listdir(backup_directory)
mms_dir = '/opt/mongodb/mms/conf'
release_dir = '/opt/mongodb/mms/mongodb-releases'
key_file = '/etc/mongodb-mms'
username = user
password = passwd
HOSTNAME = str(socket.gethostname())
dump = '/usr/local/bin/mongodump'

# recycle files greater than 7 days
for i in archives:
    filepath = os.path.join(backup_dir, i)
    if os.path.isfile(filepath):

        t = os.stat(filepath)
        # record the last time this file was changed
        c = t.st_ctime
        # delete files if older than retention_time
        if c < old_time:
            os.remove(filepath)


#setup a send mail service to use for email alerts
def sendMailAlert(error):
    SERVER = smtp_server
    FROM = HOSTNAME + "@equifax.com"
    TO = smtp_alert_recipient
    SUBJECT = HOSTNAME + ' ' + 'Error Occured: %s' % error
    TEXT = 'Script failed with error: ' + HOSTNAME + '%s' % (error)

    # Prepare actual message
    message = """\
    From: %s
    To: %s
    Subject: %s %s """ % (FROM, TO , SUBJECT, Text)
    message = "From: %s\nTo: %s\nSubject: %s\n\n%s" % ( FROM, TO, SUBJECT, TEXT )

    # Send the mail
    server = smtplib.SMTP(SERVER)
    server.sendmail(FROM, TO, message)
    server.quit()

def zip_files(src_files, dst_files):
    """ This will zip all contents of a folder
    including empty contents.
    """
    # lets get the path of the folder contents
    directory = os.walk(src_files)
    try:
        if os.path.exists(src_files):
            # grap a zip handler in write mode and name him ziph
            ziph = zipfile.ZipFile(dst_files, 'w')

            rootdir = os.path.basename(src_files)

            for root, subfolders, files in directory:
                # compress and archive all files in the base directory
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    parent_path = os.path.relpath(file_path, src_files)
                    arcname = os.path.join(rootdir, parent_path)
                    ziph.write(file_path, arcname, compress_type=zipfile.ZIP_DEFLATED)

    except IOError, error:
        send = sendMailAlert(error)
        sys.exit(1)
    except OSError, error:
        send = sendMailAlert(error)
        sys.exit(1)
    except zipfile.BadZipfile, error:
        send = sendMailAlert(error)
        sys.exit(1)
    finally:
        ziph.close()


def run_backup():
    """ This script will execute a point in time backcup """

   # make sure that you are in the backup directory prior to copying files
    cdir = os.getcwd()
    if cdir != backup_dir:
        os.chdir(backup_dir)

        for db in opsmgr_dbs:
            regex = re.compile('.*\/')
            m = regex.match(db)
            dump_name = m.group().strip('/')
            args = [dump, '--host', db, '-u', username, '-p', password, '--archive=%s' % dump_name + '-' + mod_time + '.gz', '--gzip', \
            '--readPreference=secondary', '--oplog', '--authenticationDatabase=admin']
            try:
                call(args)
            except Exception as e:
                send = sendMailAlert('%s failed to take dump. Error message: %s' % (db_name, e))

    if os.path.exists(mms_dir):
        conf_name = 'conf_archives-' + mod_time + '.zip'
        back_files = os.path.join(backup_dir, conf_name)
        zip_files(mms_dir, back_files)
    else:
        raise OSError('directory %s was removed' % mms_dir)
        send = sendMailAlert('directory %s was removed' % mms_dir)

    # copying mongodb-releases
    if os.path.exists(release_dir):
        mongodb = 'mongod_binaries-' + mod_time + '.zip'
        mongod_binaries = os.path.join(backup_dir,  mongodb)
        zip_files(release_dir, mongod_binaries)
    else:
        raise OSError('directory %s was removed' % release_dir)
        send = sendMailAlert('directory %s was removed' % release_dir)

    if os.path.exists(key_file):
        src = os.path.join(key_file, 'gen.key')
        genkey = 'gen.key-' + mod_time + '.zip'
        dst = os.path.join(backup_dir, genkey)
        zip_files(key_file, dst)
    else:
        raise OSError('directory %s was removed' % key_file)
        send = sendMailAlert('directory %s was removed' % key_file)

    #check if permission of gen.key is 640 and send eamil if its not
    key = os.path.join(key_file, 'gen.key')
    mode = oct(os.stat(key).st_mode)[-3:]
    if mode != '600':
        mode_alert = sendMailAlert('Alert: gen.key file has permission %s ' % mode)


if __name__ == "__main__":
    # catch all exceptions
    try:
        run = run_backup()
    except CalledProcessError as e:
        send = sendMailAlert(e)
        sys.exit(1)
    except OSError:
        send = sendMailAlert('There was a failure in the opsmgr_backup.py script at %s ' % traceback.format_exc())
        sys.exit(1)
    except NameError as e:
        send = sendMailAlert('Invalid variable defined at %s ' % e)
    except TypeError as e:
        send = sendMailAlert(e)
