import smtplib
import socket
from ConfigParser import RawConfigParser

config = RawConfigParser()
config.read('mongobackup.conf')
SERVER = config.get('SMTP', 'smtp_server')
smtp_alert_recipient = config.get('SMTP', 'smtp_alert_recipient')
smtp_sender = str(config.get('SMTP', 'smtp_sender'))
smtp_server = config.get('SMTP', 'smtp_server')
domain_name = config.get('SMTP', 'domain_name')
HOSTNAME = str(socket.gethostname())

def sendMailAlert(error):
    FROM = smtp_sender
    TO = smtp_alert_recipient
    SUBJECT = HOSTNAME + ' ' + 'mongoDB backup error: {}'.format(error)
    TEXT = 'Script failed with error: ' + HOSTNAME + '{}'.format(error)

    # Prepare actual message
    message = """\
    From: %s
    To: %s
    Subject: {} """.format(SUBJECT)
    message = "From: {}\nTo: {}\nSubject: {}\n\n{}".format( FROM, TO, SUBJECT, TEXT )

    # Send the mail
    server = smtplib.SMTP(SERVER)
    server.sendmail(FROM, TO, message)
    server.quit()
