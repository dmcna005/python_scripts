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

__author__ = 'Dwayne McNab'
__version__ = '1.0.2'

# Do not make changes to this script. Instead use the mongobackup.conf file
# to overwrite the following variables
config = RawConfigParser()
config.read('backup.conf')
host = config.get('BACKUPDB', 'host')
backup_directory = config.get('DEFAULT', 'backup_directory')
retention_time = int(config.get('DEFAULT', 'retention_time'))
smtp_server = conf.get('DEFAULT', 'smtp_server')
smtp_alert_recipient = conf.get('DEFAULT', 'smtp_alert_recipient')

# Because I want the name to be shorter :)
backup_dir = backup_directory

if not os.path.exists(backup_dir):
    raise OSError('backup directory does not exist! exiting script')
    send = sendMailAlert('backup directory %s does not exists!!' % backup_dir)
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
    FROM = HOSTNAME + from_domain
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

    if os.path.exists(backup_dir):
        backup_name = 'mongodb-backup' + mod_time + '.zip'
        back_files = os.path.join(backup_dir, backup_name)
        zip_files(backup_dir, back_files)
    else:
        raise OSError('directory %s was removed' % mms_dir)
        send = sendMailAlert('directory %s does not exists!!' % backup_dir)


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
