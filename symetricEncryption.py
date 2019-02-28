#!/usr/bin/env python
#from Crypto import Random
from Crypto.Cipher import AES
import hashlib, os, base64

PADDING = '{'
BLOCK_SIZE = 32
pad = lambda s: s + (BLOCK_SIZE - len(s) % BLOCK_SIZE) * PADDING
secret = os.urandom(BLOCK_SIZE)
key = hashlib.sha256(secret).digest()
cipher = AES.new(key)


def sym_encryption():
    user = str(raw_input('Enter the username to encrypt: '))
    password = str(raw_input('enter the password to encrypt: '))
    encodeAES = lambda c, s: base64.b64encode(c.encrypt(pad(s)))
    e_username = encodeAES(cipher, user)
    e_passwd = encodeAES(cipher, password)

    with open(os.path.join('/admin/keyfile.txt'), 'w') as f:
        f.write(key)

    with open(os.path.join('/admin/username.txt'), 'w') as f:
        f.write(e_username)

    with open(os.path.join('/admin/passwd.txt'), 'w') as f:
        f.write(e_passwd)



if __name__ == '__main__':
    run_encryption = sym_encryption()
