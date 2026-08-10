from datetime import datetime

def datetime_serializer(obj):
    """
    JSON序列化器，用于处理datetime类型
    
    Args:
        obj: 需要序列化的对象
        
    Returns:
        str: 序列化后的字符串
        
    Raises:
        TypeError: 如果对象类型不可序列化
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)