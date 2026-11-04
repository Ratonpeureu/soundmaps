import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import settings

_ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hmac(secret: str, scope: str, value: str) -> str:
    return hmac.new(secret.encode(), f"{scope}:{value}".encode(), hashlib.sha256).hexdigest()


def _pepper() -> str:
    return settings.HASH_PEPPER.get_secret_value()


def _salt() -> str:
    return settings.PSEUDONYM_SALT.get_secret_value()


def _prehash(password: str) -> str:
    """Pré-hachage avec un secret serveur : une fuite de la base seule ne suffit plus à attaquer les mots de passe."""
    return hmac.new(_pepper().encode(), password.encode(), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    return _ph.hash(_prehash(password))


def verify_password_ex(password_hash: str, password: str) -> tuple[bool, bool]:
    """(valide, ancien_schéma). L'ancien schéma (Argon2 sans pepper) reste accepté puis est migré."""
    try:
        if _ph.verify(password_hash, _prehash(password)):
            return True, False
    except InvalidHashError:
        return False, False
    except VerificationError:
        pass
    try:
        if _ph.verify(password_hash, password):
            return True, True
    except (VerificationError, InvalidHashError):
        pass
    return False, False


def verify_password(password_hash: str, password: str) -> bool:
    return verify_password_ex(password_hash, password)[0]


def password_needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def hash_email(tenant_id: uuid.UUID, email: str) -> str:
    return _hmac(_pepper(), f"email:{tenant_id}", email.strip().lower())


def hash_device(tenant_id: uuid.UUID, install_id: str) -> str:
    return _hmac(_pepper(), f"device:{tenant_id}", install_id.strip())


def hash_ip(ip: str) -> str:
    return _hmac(_pepper(), "ip", ip)


def make_pseudonym(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return "Contrib-" + _hmac(_salt(), f"pseudo:{tenant_id}", str(user_id))[:10]


def contributor_token(tenant_id: uuid.UUID, user_id: uuid.UUID, period: str) -> str:
    """Jeton pseudonyme à rotation mensuelle, utilisé pour compter les contributeurs distincts."""
    return _hmac(_salt(), f"contrib:{tenant_id}:{period}", str(user_id))


def new_code(length: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def recovery_hash(user_id: uuid.UUID, code: str) -> str:
    return _hmac(_pepper(), f"recovery:{user_id}", code.strip().upper().replace("-", "").replace(" ", ""))


def create_token(token_type: str, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> tuple[str, str, int]:
    ttl = (timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES) if token_type == "access"
           else timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))
    now = datetime.now(timezone.utc)
    jti = uuid.uuid4().hex
    claims = {"sub": str(user_id), "tid": str(tenant_id), "role": role, "typ": token_type,
              "jti": jti, "iat": now, "exp": now + ttl}
    token = jwt.encode(claims, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM)
    return token, jti, int(ttl.total_seconds())


def decode_token(token: str, expected_type: str) -> dict:
    claims = jwt.decode(token, settings.JWT_SECRET_KEY.get_secret_value(),
                        algorithms=[settings.JWT_ALGORITHM],
                        options={"require": ["exp", "sub", "tid", "jti", "typ"]})
    if claims.get("typ") != expected_type:
        raise jwt.InvalidTokenError("type de jeton invalide")
    return claims