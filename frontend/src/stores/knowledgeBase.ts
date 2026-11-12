import { create } from 'zustand';
import { api } from '@/api/client';
import type { KnowledgeSpace, ReadScope } from '@/api/domains/knowledgeBase';

interface KnowledgeBaseState {
  spaces: KnowledgeSpace[];
  loading: boolean;
  error: string | null;
  load: (subjectTag?: string) => Promise<void>;
  upsert: (space: KnowledgeSpace) => void;
  create: (payload: {
    name: string;
    read_scope: ReadScope;
    subject_tags: string[];
  }) => Promise<KnowledgeSpace>;
  update: (spaceId: string, payload: Parameters<typeof api.knowledgeBase.updateSpace>[1]) => Promise<KnowledgeSpace>;
}

/** 知识空间列表的单一来源：列表页与详情页改策略后同步刷新。 */
export const useKnowledgeBaseStore = create<KnowledgeBaseState>((set, get) => ({
  spaces: [],
  loading: false,
  error: null,
  load: async (subjectTag) => {
    set({ loading: true, error: null });
    try {
      const spaces = await api.knowledgeBase.listSpaces(subjectTag);
      set({ spaces, loading: false });
    } catch (err) {
      set({
        loading: false,
        error: err instanceof Error ? err.message : '知识库加载失败',
      });
    }
  },
  upsert: (space) =>
    set((s) => ({
      spaces: s.spaces.some((item) => item.id === space.id)
        ? s.spaces.map((item) => (item.id === space.id ? space : item))
        : [...s.spaces, space],
    })),
  create: async (payload) => {
    const space = await api.knowledgeBase.createSpace(payload);
    get().upsert(space);
    return space;
  },
  update: async (spaceId, payload) => {
    const space = await api.knowledgeBase.updateSpace(spaceId, payload);
    get().upsert(space);
    return space;
  },
}));
