import { create } from 'zustand';

export type MaterialType = 'teaching_syllabus' | 'assessment_syllabus' | 'teaching_material' | 'exercise';

export interface Course {
  id: string;
  owner_id: string;
  name: string;
  slug: string;
  description: string | null;
}

interface CourseState {
  courses: Course[];
  activeCourseId: string | null;
  setCourses: (courses: Course[]) => void;
  setActiveCourse: (courseId: string) => void;
  addCourse: (course: Course) => void;
  removeCourse: (courseId: string) => void;
}

export const useCourseStore = create<CourseState>((set) => ({
  courses: [],
  activeCourseId: null,
  setCourses: (courses) => set({ courses, activeCourseId: courses[0]?.id ?? null }),
  setActiveCourse: (activeCourseId) => set({ activeCourseId }),
  // 幂等 upsert：同 id 已存在则原位更新（Sidebar 补拉与课程空间列表并发时防重复条目）
  addCourse: (course) =>
    set((s) => ({
      courses: s.courses.some((c) => c.id === course.id)
        ? s.courses.map((c) => (c.id === course.id ? course : c))
        : [...s.courses, course],
    })),
  // 删除课程后从列表移除；被删的恰是当前活跃课程则清空活跃标记
  removeCourse: (courseId) =>
    set((s) => ({
      courses: s.courses.filter((c) => c.id !== courseId),
      activeCourseId: s.activeCourseId === courseId ? null : s.activeCourseId,
    })),
}));
