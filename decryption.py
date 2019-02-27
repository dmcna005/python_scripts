from Crypto.Cipher import AES
import base64

with open('admin/username.txt') as f:
    user_name = f.read()

with open('admin/passwd.txt') as f:
    password = f.read()

with open('admin/keyfile.txt') as f:
    key = f.read()

PADDING = '{'
cipher = AES.new(key)

decodeAES = lambda c, e: c.decrypt(base64.b64decode(e)).rstrip(PADDING)
user = decodeAES(cipher, user_name)
passwd = decodeAES(cipher, password)
