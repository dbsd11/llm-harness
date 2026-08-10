import os
import bcrypt
from database.repositories.user_repository import UserRepository

from logger import logger

auth_message = "APP-TEMPLATE"

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def auth(username, password):
    admin_username = os.getenv("ADMIN_USERNAME")
    admin_password = os.getenv("ADMIN_PASSWORD")
    if admin_username and admin_password and username == admin_username and password == admin_password:
        return True

    user_repo = UserRepository()
    user = user_repo.find_by_username(username)

    if not user:
        return False

    try:
        return bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8'))
    except (ValueError, TypeError):
        return False

def register_user(username: str, email: str, password: str, **kwargs) -> bool:
    try:
        user_repo = UserRepository()

        existing_user = user_repo.find_by_username(username)
        if existing_user:
            return False

        from datetime import datetime
        from database.models.user import User

        password_hash = hash_password(password)

        user = User(
            username=username,
            email=email,
            password_hash=password_hash,
            is_active=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            **kwargs
        )

        user_id = user_repo.create(user)
        return user_id is not None
    except Exception as e:
        logger.error(f"注册用户失败: {str(e)}")
        return False
