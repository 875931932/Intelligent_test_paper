import { request } from '../http';
import type { LoginResponse, UserProfile } from '../../types/api';

/** 管理员建号入参（本系统不开放自助注册，账号一律由管理员创建） */
export interface CreateUserPayload {
  username: string;
  password: string;
  name: string;
}

export const authApi = {
  login: (username: string, password: string): Promise<LoginResponse> =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),
  me: (token?: string): Promise<UserProfile> => request<UserProfile>('/auth/me', undefined, token),
  /** 管理员建号：仅管理员可用，新账号固定教师角色 */
  createUser: (payload: CreateUserPayload, token?: string): Promise<UserProfile> =>
    request<UserProfile>('/auth/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }, token),
};