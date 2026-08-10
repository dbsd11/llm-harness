# 用户仓储
from typing import List, Optional
from datetime import datetime
import os
from logger import logger

from ..models.local_user import User
from .base_repository import BaseRepository
from ..local_connection import get_connection_manager

class UserRepository(BaseRepository[User]):
    """用户仓储"""
    
    def __init__(self):
        super().__init__(User)
    
    def find_by_username(self, username: str) -> Optional[User]:
        """根据用户名查找用户"""
        users = self.find_by_criteria({"username": username})
        return users[0] if users else None
    
    def find_by_email(self, email: str) -> Optional[User]:
        """根据邮箱查找用户"""
        users = self.find_by_criteria({"email": email})
        return users[0] if users else None
    
    def sync_from_member_user(self):
        """
        从member_user表增量同步用户数据到user表

        返回:
            dict: 包含同步统计信息的字典
        """
        # 获取上一次同步的时间戳
        last_sync_time = self._get_last_sync_time("member_user")
        
        # 统计信息
        stats = {
            'total': 0,
            'inserted': 0,
            'updated': 0,
            'errors': 0,
            'error_messages': []
        }
        
        try:
            # 获取源数据库连接
            connection_manager = get_connection_manager()
            
            # 查询member_user表中新增或更新的记录
            with connection_manager.get_connection() as conn:
                cursor = conn.cursor()
                
                # 构建查询SQL
                if last_sync_time:
                    # 如果有上次同步时间，只查询之后更新或创建的记录
                    sql = """
                    SELECT -1 * id as id, nickname, name, mobile, password, create_time, update_time
                    FROM yanqipei.member_user
                    WHERE update_time > %s OR create_time > %s
                    ORDER BY id ASC
                    """
                    cursor.execute(sql, (last_sync_time, last_sync_time))
                else:
                    # 如果没有上次同步时间，查询所有记录
                    sql = """
                    SELECT -1 * id as id, nickname, name, mobile, password, create_time, update_time
                    FROM yanqipei.member_user
                    ORDER BY id ASC
                    """
                    cursor.execute(sql)
                
                member_users = cursor.fetchall()
                stats['total'] = len(member_users)
                
                # 处理每一条记录
                for member_user in member_users:
                    try:
                        # 检查用户是否已存在
                        existing_user = self.find_by_id(member_user['id'])
                        
                        # 获取用户的组织信息
                        organization_id = None
                        try:
                            org_cursor = conn.cursor()
                            org_sql = """
                            SELECT -1 * org_id as org_id FROM yanqipei.member_user_organization 
                            WHERE -1 * user_id = %s AND deleted = 0
                            LIMIT 1
                            """
                            org_cursor.execute(org_sql, (member_user['id'],))
                            org_result = org_cursor.fetchone()
                            if org_result:
                                organization_id = org_result['org_id']
                            org_cursor.close()
                        except Exception as e:
                            logger.error(f"获取用户 {member_user['id']} 的组织信息失败: {str(e)}")
                        
                        # 准备用户数据
                        user_data = {
                            'id': member_user['id'],
                            'username': f"#{member_user['nickname']}" if member_user['nickname'] else f"#{member_user['name']}",
                            'email': f"{member_user['mobile']}@example.com",
                            'password_hash': os.getenv('SYNC_DEFAULT_PASSWORD_HASH', ''),
                            'is_active': True,
                            'organization_id': organization_id,
                            'created_at': member_user['create_time'],
                            'updated_at': member_user['update_time']
                        }
                        
                        if existing_user:
                            # 更新现有用户
                            for key, value in user_data.items():
                                if hasattr(existing_user, key):
                                    setattr(existing_user, key, value)
                            
                            self.update(existing_user)
                            stats['updated'] += 1
                        else:
                            # 创建新用户
                            new_user = User(**user_data)
                            self.create(new_user)
                            stats['inserted'] += 1
                            
                    except Exception as e:
                        stats['errors'] += 1
                        error_msg = f"处理用户ID {member_user['id']} 时出错: {str(e)}"
                        stats['error_messages'].append(error_msg)
                        logger.error(error_msg)
                
                # 更新同步时间戳
                if stats['total'] > 0:
                    self._update_last_sync_time("member_user")
                
        except Exception as e:
            error_msg = f"同步用户数据时出错: {str(e)}"
            stats['errors'] += 1
            stats['error_messages'].append(error_msg)
            logger.error(error_msg)
        
        return stats
    
    def _get_last_sync_time(self, source_table):
        """获取上次同步时间"""
        # 这里可以使用一个简单的文件或数据库表来记录同步时间
        # 为简化实现，使用文件记录
        import os
        sync_file = f"sync_time_{source_table}.txt"
        
        try:
            if os.path.exists(sync_file):
                with open(sync_file, 'r') as f:
                    return f.read().strip()
        except Exception as e:
            logger.error(f"读取同步时间文件出错: {str(e)}")
        
        return None
    
    def _update_last_sync_time(self, source_table):
        """更新同步时间"""
        import os
        sync_file = f"sync_time_{source_table}.txt"
        
        try:
            with open(sync_file, 'w') as f:
                f.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        except Exception as e:
            logger.error(f"更新同步时间文件出错: {str(e)}")
