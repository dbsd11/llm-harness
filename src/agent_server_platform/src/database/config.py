# 数据库配置文件
import os

# 数据库配置
DB_CONFIG = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE', 'sqlite'),  # 支持sqlite, mysql, postgresql
        'NAME': os.getenv('DB_NAME', 'cnipy_app.db'),
        'USER': os.getenv('DB_USER', ''),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', ''),
        'PORT': os.getenv('DB_PORT', ''),
    }
}

# 连接池配置
POOL_CONFIG = {
    'max_connections': int(os.getenv('DB_MAX_CONNECTIONS', 10)),
    'min_connections': int(os.getenv('DB_MIN_CONNECTIONS', 1)),
    'timeout': int(os.getenv('DB_TIMEOUT', 30)),
}