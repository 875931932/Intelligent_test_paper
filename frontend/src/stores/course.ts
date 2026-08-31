import { create } from 'zustand';
import { config } from '@/config';
import { demoCourses } from '@/mocks/demo';

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
}

// 演示模式下内置静态课程，避免空课程列表
const initialCourses: Course[] = config.enableMock
  ? demoCourses.map((c) => ({ ...c }))
  : [];

export const useCourseStore = create<CourseState>((set) => ({
  courses: initialCourses,
  activeCourseId: initialCourses[0]?.id ?? null,
  setCourses: (courses) => set({ courses, activeCourseId: courses[0]?.id ?? null }),
  setActiveCourse: (activeCourseId) => set({ activeCourseId }),
  addCourse: (course) => set((s) => ({ courses: [...s.courses, course] })),
}));
