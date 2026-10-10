import { request } from '../http';
import type { LoginResponse, UserProfile } from '../../types/api';

export const authApi = {
  login: (username: string, password: string): Promise<LoginResponse> =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    }),
  me: (token?: string): Promise<UserProfile> => request<UserProfile>('/auth/me', undefined, token),
};