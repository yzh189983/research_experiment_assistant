"""字段级凭据加密与日志脱敏。

主密钥来自 REKIT_MASTER_PASSWORD 或 data/secret.key（权限 600）。
加密值带 `enc:v1:` 前缀，迁移时可直接识别明文。
"""
import base64
import datetime
import hashlib
import os
import re

try:
    from cryptography.fernet import Fernet
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except Exception:  # pragma: no cover - 环境与依赖异常时降级为明文，不阻断启动
    Fernet = None
    x509 = None

PREFIX = "enc:v1:"
_KNOWN_SECRETS = set()


def _fernet():
    if Fernet is None:
        return None
    secret = os.environ.get("REKIT_MASTER_PASSWORD")
    if not secret:
        import config
        secret = config.get_or_create_secret_key()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def remember_secret(value):
    if value and len(str(value)) >= 6:
        _KNOWN_SECRETS.add(str(value))


def encrypt(value):
    """加密字符串；已加密或空值原样返回。依赖缺失时降级为明文。"""
    if value is None or value == "":
        return value
    value = str(value)
    if value.startswith(PREFIX):
        return value
    f = _fernet()
    if not f:
        return value
    remember_secret(value)
    return PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value):
    """解密字符串；非加密值原样返回，解密失败返回空串。"""
    if value is None or value == "":
        return value
    value = str(value)
    if not value.startswith(PREFIX):
        return value
    f = _fernet()
    if not f:
        return ""
    try:
        plain = f.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
        remember_secret(plain)
        return plain
    except Exception:
        return ""


def redact(text):
    """把已知密钥与常见凭据模式替换成掩码，用于日志 / SSE / 错误提示。"""
    s = "" if text is None else str(text)
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        s = s.replace(secret, "***REDACTED***")
    s = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***REDACTED***", s)
    s = re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "***REDACTED***", s)
    s = re.sub(r"(?i)(password|passwd|passphrase|api[_-]?key)\s*[:=]\s*\S+",
               r"\1=***REDACTED***", s)
    return s


def ensure_tls_cert(cert_path, key_path, common_name="127.0.0.1"):
    """缺失时用 cryptography 生成自签证书，供本地 HTTPS/WSS 使用。"""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    if x509 is None:
        raise RuntimeError("缺少 cryptography，无法生成 HTTPS 自签证书")
    os.makedirs(os.path.dirname(cert_path), exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "rekit-local"),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1")),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    return cert_path, key_path
