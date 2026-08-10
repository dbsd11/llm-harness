import hashlib  # 添加这一行

def compute_md5(str: str):
    return hashlib.md5(str.encode('utf-8')).hexdigest()