"""配置项对称加密（仅依赖 Python 标准库）。

面板里有些配置一旦泄露就等于把控制权交出去，最典型的是飞书机器人的
Webhook 地址（拿到即可向群里推送任意消息）和签名密钥。本模块提供一套
轻量的认证加密，用于在落库前把这类配置变成密文：

* 以 ``settings.secret_key`` 为根密钥，用 HMAC-SHA256 分别派生出加密密钥
  与认证密钥（KDF）；
* 随机 16 字节 nonce，用 HMAC-SHA256 作 PRF 生成密钥流做异或（流式加密）；
* 采用 encrypt-then-MAC，用认证密钥对 ``nonce || 密文`` 计算 16 字节标签，
  解密时用 :func:`hmac.compare_digest` 校验，防止篡改。

密文格式为 ``enc:v2:<urlsafe-base64(nonce || tag || body)>``（tag 32 字节，
密钥用 scrypt + 安装级 salt 派生）。旧版本写下的 ``enc:v1:`` 仍然可以解密
（tag 16 字节、无 salt 的 HMAC 派生），:func:`needs_rewrap` 会提示把它们重写成
v2；:func:`decrypt` 对不带前缀的内容原样返回，历史明文配置依然可用。

不引入 ``cryptography``/``Fernet`` 的原因：后端依赖保持精简，而这套实现对
"加密一段 Webhook 字符串"的场景已经足够。注意根密钥来自 ``secret_key``，
**更换 secret_key 会导致既有密文无法解密**，此时配置会退化成"未配置"，
需要重新填写。

KDF 的上下文标识（项目改名时改过一次）与 ``secret_key`` 是两回事：旧标识
保存在 :data:`_LEGACY_KDF_INFOS` 里，解密时会自动回退，因此改名不会让已存
凭据失效。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from .config import DATA_DIR, settings

PREFIX = "enc:v1:"
#: v2：密钥派生改为带迭代的 scrypt + 安装级随机 salt，标签放宽到 32 字节。
#: v1 的 ``HMAC(secret_key, info)`` 没有 salt、没有迭代，**拿到任意一段密文**
#: 就能对候选 SECRET_KEY 做一次 HMAC 验证（PVE-CONF-002）。v1 只保留读取，
#: 新写入一律 v2；读取到 v1 时由 :func:`needs_rewrap` 提示重写。
PREFIX_V2 = "enc:v2:"

#: KDF 的上下文标识，参与密钥派生：它一变，库里已存的 PVE Token / SMTP 口令 /
#: TOTP 种子、SSH 私钥都会解不开。2026-11 随项目改名换成了现在的名字，
#: 旧密文靠 :data:`_LEGACY_KDF_INFOS` 回退解密，不需要用户重新填写。
#: **此后不要再改它**。
_KDF_INFO = b"pvecenter/config-encryption/v1"
#: 只用于**解密**的历史 KDF 标识（旧项目名时期）。项目改名时 KDF 标识跟着改，
#: 若不回退，升级后所有已保存凭据都会变成「未配置」—— 典型表现是 AI 终端报
#: ``No authentication methods available``、PVE 连接突然 401。加密始终用新标识。
#: 以 base64 存放：这串旧标识只需逐字节还原，写成明文会让仓库里残留旧品牌名。
_LEGACY_KDF_INFOS = (
    base64.b64decode("cHJveGNlbnRlci9jb25maWctZW5jcnlwdGlvbi92MQ=="),
)
_NONCE_LEN = 16
_TAG_LEN = 16
_TAG_LEN_V2 = 32
_SALT_LEN = 16
#: scrypt 参数：n=2^14 约 16MB 内存、单次 ~50ms。只在本进程首次加解密时付一次
#: （结果缓存在 :data:`_KEY_CACHE`），之后都是字典命中。
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_CACHE: dict = {}
_SALT_CACHE: list = []
#: 安装级 salt 落在数据目录（不进数据库：数据库备份泄露时它不该跟着走）
_SALT_FILE = DATA_DIR / ".enc-salt"


def _salt() -> bytes:
    """读取（或首次生成）安装级 salt。

    文件权限 0600；用 ``O_CREAT|O_EXCL`` 创建，多 worker 同时首启也只会有一份。
    """
    if _SALT_CACHE:
        return _SALT_CACHE[0]
    try:
        salt = _SALT_FILE.read_bytes()
    except OSError:
        salt = b""
    if not salt:
        candidate = os.urandom(_SALT_LEN)
        try:
            _SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(_SALT_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # 另一个进程刚建好：用它写下的那一份
            try:
                salt = _SALT_FILE.read_bytes()
            except OSError:
                salt = candidate
        except OSError:
            salt = candidate
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(candidate)
            salt = candidate
    _SALT_CACHE.clear()
    _SALT_CACHE.append(salt)
    return salt


def _derive_keys_v2(master: bytes, salt: bytes) -> tuple:
    """v2 的密钥派生：scrypt(master, 安装级 salt) 出 64 字节，前 32 加密、后 32 认MAC。"""
    cached = _KEY_CACHE.get((master, salt))
    if cached is not None:
        return cached
    material = hashlib.scrypt(
        master, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=64
    )
    keys = (material[:32], material[32:])
    if len(_KEY_CACHE) > 8:  # 轮换密钥时也就几条，防止被测试无限撑大
        _KEY_CACHE.clear()
    _KEY_CACHE[(master, salt)] = keys
    return keys


def _derive_keys(info: bytes = _KDF_INFO, master_key: bytes = b"") -> tuple:
    """从面板 ``secret_key`` 与 KDF 上下文派生 (加密密钥, 认证密钥)。"""
    master = master_key or (settings.secret_key or "").encode("utf-8")
    enc_key = hmac.new(master, info + b"|enc", hashlib.sha256).digest()
    mac_key = hmac.new(master, info + b"|mac", hashlib.sha256).digest()
    return enc_key, mac_key


def _masters() -> list:
    """当前根密钥 + 轮换期保留的旧密钥（``SECRET_KEY_PREVIOUS``，逗号分隔）。

    只用于**解密**：新写入始终用当前 ``secret_key``。这样轮换密钥时不会出现
    「换了根密钥 → 全部凭据静默变成未配置」的情况。
    """
    current = (settings.secret_key or "").encode("utf-8")
    masters = [current] if current else []
    for item in str(getattr(settings, "secret_key_previous", "") or "").split(","):
        value = item.strip().encode("utf-8")
        if value and value not in masters:
            masters.append(value)
    return masters


def _current_master() -> bytes:
    return (settings.secret_key or "").encode("utf-8")


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(
            hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, key: bytes, nonce: bytes) -> bytes:
    stream = _keystream(key, nonce, len(data))
    return bytes(a ^ b for a, b in zip(data, stream))


def is_encrypted(value: str) -> bool:
    """判断字符串是否为本模块产出的密文（v1 与 v2 都算）。"""
    return isinstance(value, str) and value.startswith((PREFIX, PREFIX_V2))


def encrypt(plain: str) -> str:
    """加密字符串（始终写 v2）；空串原样返回（避免给"未配置"也套一层密文）。"""
    if not plain:
        return ""
    if is_encrypted(plain):
        return plain
    enc_key, mac_key = _derive_keys_v2(_current_master(), _salt())
    nonce = os.urandom(_NONCE_LEN)
    body = _xor(plain.encode("utf-8"), enc_key, nonce)
    tag = hmac.new(mac_key, nonce + body, hashlib.sha256).digest()[:_TAG_LEN_V2]
    token = base64.urlsafe_b64encode(nonce + tag + body).decode("ascii")
    return PREFIX_V2 + token


def decrypt(value: str) -> str:
    """解密 :func:`encrypt` 的结果。

    非密文（历史明文）原样返回；密文损坏、被篡改或根密钥已更换时返回空串，
    由调用方按"未配置"处理。

    先按当前 KDF 标识验签，失败再按 :data:`_LEGACY_KDF_INFOS` 逐个重试 ——
    项目改名只改了上下文标识，``secret_key`` 本身没变，旧密文仍然可以还原。
    这样升级后不需要用户重新填写 PVE Token / SSH 私钥 / SMTP 口令 / TOTP 种子。
    """
    if not value:
        return ""
    if not is_encrypted(value):
        return value
    if value.startswith(PREFIX_V2):
        return _decrypt_v2(value)
    try:
        raw = base64.urlsafe_b64decode(value[len(PREFIX):].encode("ascii"))
    except (ValueError, TypeError):
        return ""
    if len(raw) <= _NONCE_LEN + _TAG_LEN:
        return ""
    nonce = raw[:_NONCE_LEN]
    tag = raw[_NONCE_LEN:_NONCE_LEN + _TAG_LEN]
    body = raw[_NONCE_LEN + _TAG_LEN:]
    for master in _masters():
        for info in (_KDF_INFO, *_LEGACY_KDF_INFOS):
            enc_key, mac_key = _derive_keys(info, master)
            expected = hmac.new(mac_key, nonce + body, hashlib.sha256).digest()[:_TAG_LEN]
            if hmac.compare_digest(tag, expected):
                return _xor(body, enc_key, nonce).decode("utf-8", "replace")
    return ""


def _decrypt_v2(value: str) -> str:
    """解 v2 密文：scrypt 派生的密钥 + 32 字节标签。"""
    try:
        raw = base64.urlsafe_b64decode(value[len(PREFIX_V2):].encode("ascii"))
    except (ValueError, TypeError):
        return ""
    if len(raw) <= _NONCE_LEN + _TAG_LEN_V2:
        return ""
    nonce = raw[:_NONCE_LEN]
    tag = raw[_NONCE_LEN:_NONCE_LEN + _TAG_LEN_V2]
    body = raw[_NONCE_LEN + _TAG_LEN_V2:]
    salt = _salt()
    for master in _masters():
        enc_key, mac_key = _derive_keys_v2(master, salt)
        expected = hmac.new(mac_key, nonce + body, hashlib.sha256).digest()
        if hmac.compare_digest(tag, expected):
            return _xor(body, enc_key, nonce).decode("utf-8", "replace")
    return ""


def needs_rewrap(value: str) -> bool:
    """密文是否需要用当前根密钥重写。

    两种情况需要重写：旧根密钥加密的（``SECRET_KEY_PREVIOUS`` 能解开、当前
    密钥解不开），以及旧 KDF 标识加密的。明文与空值返回 ``False``。
    """
    if not is_encrypted(value):
        return False
    if value.startswith(PREFIX):
        # v1 一律重写成 v2：v1 的派生无 salt / 无迭代，留着就等于留着离线验证面
        return bool(decrypt(value))
    plain = decrypt(value)
    if not plain:
        # 解不开的密文无从判断，交给运维排查；这里不谎报「需要重写」
        return False
    return _decrypt_with_current(value) != plain


def _decrypt_with_current(value: str) -> str:
    """只用当前根密钥解密（用于判断是否需要重写）。"""
    if value.startswith(PREFIX_V2):
        raw = base64.urlsafe_b64decode(value[len(PREFIX_V2):].encode("ascii")) if value else b""
        if len(raw) <= _NONCE_LEN + _TAG_LEN_V2:
            return ""
        nonce = raw[:_NONCE_LEN]
        tag = raw[_NONCE_LEN:_NONCE_LEN + _TAG_LEN_V2]
        body = raw[_NONCE_LEN + _TAG_LEN_V2:]
        enc_key, mac_key = _derive_keys_v2(_current_master(), _salt())
        expected = hmac.new(mac_key, nonce + body, hashlib.sha256).digest()
        if hmac.compare_digest(tag, expected):
            return _xor(body, enc_key, nonce).decode("utf-8", "replace")
        return ""
    try:
        raw = base64.urlsafe_b64decode(value[len(PREFIX):].encode("ascii"))
    except (ValueError, TypeError):
        return ""
    if len(raw) <= _NONCE_LEN + _TAG_LEN:
        return ""
    nonce = raw[:_NONCE_LEN]
    tag = raw[_NONCE_LEN:_NONCE_LEN + _TAG_LEN]
    body = raw[_NONCE_LEN + _TAG_LEN:]
    for info in (_KDF_INFO,):
        enc_key, mac_key = _derive_keys(info, _current_master())
        expected = hmac.new(mac_key, nonce + body, hashlib.sha256).digest()[:_TAG_LEN]
        if hmac.compare_digest(tag, expected):
            return _xor(body, enc_key, nonce).decode("utf-8", "replace")
    return ""


def rewrap(value: str) -> str:
    """用当前根密钥重写一段密文；明文 / 空值 / 解不开的密文原样返回。"""
    if not is_encrypted(value):
        return value
    plain = decrypt(value)
    if not plain:
        return value
    return encrypt(plain)


def mask(value: str, head: int = 4, tail: int = 4) -> str:
    """打码展示：保留首尾各若干字符，中间用 ``*`` 代替。"""
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * 8
    return value[:head] + "*" * 6 + value[-tail:]
