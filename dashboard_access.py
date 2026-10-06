"""HTTPS LAN access with expiring password sessions."""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import ssl
import time
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.parse import urlparse

class Access:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'work'/'wifi-access.json';self.sessions={};self.failures={}
        self.cloud_mode=os.environ.get('ONYX_CLOUD_MODE','').lower() in ('1','true','yes')
        self.require_login=self.cloud_mode or os.environ.get('ONYX_REQUIRE_LOGIN','').lower() in ('1','true','yes')
        self._provision_cloud_password()
    def _provision_cloud_password(self):
        """Create the persistent password verifier once; never store the password in runtime state."""
        password=os.environ.get('DASHBOARD_PASSWORD','')
        if not self.require_login or self.path.exists() or not password:return
        self.path.parent.mkdir(parents=True,exist_ok=True);salt=secrets.token_bytes(16)
        public=urlparse(os.environ.get('ONYX_PUBLIC_ORIGIN',''))
        configured=[value.strip() for value in os.environ.get('ONYX_ALLOWED_HOSTS','').split(',') if value.strip()]
        hosts=[value for value in ('localhost','127.0.0.1',public.hostname,*configured) if value]
        cfg={'enabled':False,'hosts':hosts,'salt':salt.hex(),'hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,300000).hex()}
        temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps(cfg));temporary.chmod(0o600);temporary.replace(self.path)
    def settings(self):
        try:return json.loads(self.path.read_text())
        except (OSError,ValueError):return {}
    def authenticated(self,handler):
        if not self.require_login and handler.client_address[0] in ('127.0.0.1','::1') and not isinstance(handler.connection,ssl.SSLSocket):return True
        cookie=SimpleCookie()
        try:cookie.load(handler.headers.get('Cookie',''));value=cookie.get('onyx_session')
        except Exception:return False
        return bool(value and self.sessions.get(value.value,0)>time.monotonic())
    def login(self,password,ip):
        failures=self.failures.get(ip,[]);failures=[t for t in failures if time.monotonic()-t<600]
        if len(failures)>=10:return None
        cfg=self.settings()
        derived=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(cfg.get('salt','')),300000).hex()
        if not cfg.get('hash') or not hmac.compare_digest(derived,cfg['hash']):
            failures.append(time.monotonic());self.failures[ip]=failures;return None
        token=secrets.token_urlsafe(32);self.sessions[token]=time.monotonic()+12*3600;self.failures.pop(ip,None);return token
    def hosts(self):
        public=urlparse(os.environ.get('ONYX_PUBLIC_ORIGIN',''))
        cloud={public.netloc} if public.netloc else set()
        configured={value.strip() for value in os.environ.get('ONYX_ALLOWED_HOSTS','').split(',') if value.strip()}
        return {'127.0.0.1:8765','localhost:8765','127.0.0.1:8766','localhost:8766',*cloud,*configured,*[host+':8766' for host in self.settings().get('hosts',[])]}
    def context(self):
        cfg=self.settings()
        if not cfg.get('enabled'):return None
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.root/'work'/'certificates'/'server.pem',self.root/'work'/'certificates'/'server-key.pem');return context

def setup(root,addresses,refresh_certificates=False):
    from cryptography import x509
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from datetime import datetime,timedelta,timezone
    root=Path(root)
    existing=(root/'work'/'wifi-access.json').exists()
    if existing and not refresh_certificates:return
    folder=root/'work'/'certificates';folder.mkdir(parents=True,exist_ok=True)
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    now=datetime.now(timezone.utc);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Onyx and Ink local dashboard CA')])
    ca=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=3650)).add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).add_extension(x509.KeyUsage(digital_signature=True,key_encipherment=False,key_cert_sign=True,crl_sign=True,content_commitment=False,data_encipherment=False,key_agreement=False,encipher_only=False,decipher_only=False),critical=True).sign(ca_key,hashes.SHA256())
    hosts=list(dict.fromkeys(['localhost','127.0.0.1',*addresses]));sans=[]
    for host in hosts:
        try:sans.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:sans.append(x509.DNSName(host))
    cert=x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Onyx and Ink staff dashboard')])).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365)).add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False).add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),critical=False).add_extension(x509.KeyUsage(digital_signature=True,key_encipherment=True,key_cert_sign=False,crl_sign=False,content_commitment=False,data_encipherment=False,key_agreement=False,encipher_only=False,decipher_only=False),critical=True).add_extension(x509.SubjectAlternativeName(sans),critical=False).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).sign(ca_key,hashes.SHA256())
    for filename,data in [('server.pem',cert.public_bytes(serialization.Encoding.PEM)),('server-key.pem',key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())),('onyx-dashboard-ca.crt',ca.public_bytes(serialization.Encoding.PEM))]:
        path=folder/filename;path.write_bytes(data);path.chmod(0o600 if 'key' in filename else 0o644)
    if existing:return
    password=secrets.token_urlsafe(18);salt=secrets.token_bytes(16)
    cfg={'enabled':True,'hosts':hosts,'salt':salt.hex(),'hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt,300000).hex()}
    path=root/'work'/'wifi-access.json';path.write_text(json.dumps(cfg));path.chmod(0o600)
    local=root/'work'/'wifi-password.txt';local.write_text(password+'\n');local.chmod(0o600)
