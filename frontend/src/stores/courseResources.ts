import { create } from 'zustand';
import { api } from '@/api/client';
import type { CourseMaterialResource } from '@/api/domains/knowledgeBase';
import { useAuthStore } from './auth';

interface ResourceState {
  ownerId: string | null;
  items: CourseMaterialResource[];
  loading: boolean;
  error: string | null;
  load: () => Promise<void>;
}

let requestNo = 0;
export const useCourseResourcesStore = create<ResourceState>((set) => ({
  ownerId: null, items: [], loading: false, error: null,
  load: async () => {
    const ownerId = useAuthStore.getState().user?.id ?? null;
    const current = ++requestNo;
    set({ ownerId, items: [], loading: true, error: null });
    try {
      const items = await api.knowledgeBase.listCourseMaterials();
      if (current === requestNo && useAuthStore.getState().user?.id === ownerId) set({ items, loading: false });
    } catch (err) {
      if (current === requestNo) set({ loading: false, error: err instanceof Error ? err.message : '读取资料失败' });
    }
  },
}));
