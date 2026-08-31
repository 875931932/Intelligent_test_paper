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
}

export const useCourseStore = create<CourseState>((set) => ({
  courses: [],
  activeCourseId: null,
  setCourses: (courses) => set({ courses, activeCourseId: courses[0]?.id ?? null }),
  setActiveCourse: (activeCourseId) => set({ activeCourseId }),
  addCourse: (course) => set((s) => ({ courses: [...s.courses, course] })),
}));
