#!/usr/bin/env python
import os
from base64 import urlsafe_b64encode, urlsafe_b64decode
from cryptography.fernet import Fernet

user = str(raw_input('Enter your username: '))
user1 = user.encode()
password = str(raw_input('Enter your password: '))
password1 = password.encode()
isadmin_dir = os.path.isdir('admin')
# relative path to use for your user and password files
#relative_dir = os.path.dirname(os.path.realpath('__file__'))

# generates random encryption key
e_key = Fernet.generate_key()
f_key = Fernet(e_key)
e_username = f_key.encrypt(user1)
e_passwd = f_key.encrypt(password1)

def super_encryption():
    if isadmin_dir == False:
        os.mkdir('/admin')

    with open(os.path.join('/admin', 'keyfile.txt'), 'w') as keyfile:
        keyfile.write(f_key)

    with open(os.path.join('/admin', 'username.txt'), 'w') as f:
        f.write(e_username)

    with open(os.path.join('/admin', 'passwd.txt'), 'w') as f:
        f.write(e_passwd)


if __name__ == '__main__':
    run = super_encryption()
