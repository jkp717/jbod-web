#!/usr/bin/env python3

import os
import base64
from cryptography.fernet import Fernet


basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


if __name__ == '__main__':
    if not os.path.exists(os.path.join(basedir, '.env')):
        with open(os.path.join(basedir, '.env'), 'w') as f:
            f.write(f"SECRET_KEY={Fernet.generate_key().decode()}\n")
            f.write(f"SECRET_KEY_SALT={base64.b64encode(os.urandom(16)).decode()}\n")