import { create } from 'zustand';

export interface User {
  id: string;
  username: string;
  name: string;
  role: 'teacher' | 'admin';
}

interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  setAuth: (user: User, token: string) => void;
  logout: () => void;
}

const STORAGE_KEY = 'exam_auth';

function loadStored(): { user: User | null; token: string | null } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { user: null, token: null };
    return JSON.parse(raw);
  } catch {
    return { user: null, token: null };
  }
}

function storeAuth(user: User, token: string) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ user, token }));
}

export const useAuthStore = create<AuthState>((set) => {
  const stored = loadStored();
  return {
    user: stored.user,
    token: stored.token,
    isAuthenticated: !!stored.user,
    setAuth: (user, token) => {
      storeAuth(user, token);
      set({ user, token, isAuthenticated: true });
    },
    logout: () => {
      localStorage.removeItem(STORAGE_KEY);
      set({ user: null, token: null, isAuthenticated: false });
    },
  };
});
