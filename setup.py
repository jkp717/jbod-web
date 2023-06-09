from setuptools import setup, find_packages

setup(
    name='jbod-web',
    version='1.0.0',
    author='Josh Price',
    author_email='joshkprice717@gmail.com',
    description='Web interface for managing JBOD Controllers',
    packages=find_packages(),
    install_requires=[
        'pyserial >= 3.5',
        'flask',
        'requests',
        'flask_apscheduler',
        'apscheduler',
        'flask_admin',
        'flask_sqlalchemy',
        'cryptography',
        'sqlalchemy',
        'wtforms',
        'python-dotenv'
    ],
)

# run 'python setup.py install'