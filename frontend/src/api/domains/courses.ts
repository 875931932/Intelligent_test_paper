import { request } from '../http';
import type { CourseCategoryInfo, CourseCreate, CourseResponse, CourseUpdate } from '../../types/api';

export const coursesApi = {
  list: (token?: string): Promise<CourseResponse[]> =>
    request('/courses', undefined, token),
  /** 课程类别清单（创建课程时选择；必须先于 /{course_id} 注册，后端已保证） */
  categories: (token?: string): Promise<CourseCategoryInfo[]> =>
    request('/courses/categories', undefined, token),
  create: (data: CourseCreate, token?: string): Promise<CourseResponse> =>
    request('/courses', { method: 'POST', body: JSON.stringify(data) }, token),
  get: (courseId: string, token?: string): Promise<CourseResponse> =>
    request('/courses/' + courseId, undefined, token),
  update: (courseId: string, data: CourseUpdate, token?: string): Promise<CourseResponse> =>
    request('/courses/' + courseId, { method: 'PATCH', body: JSON.stringify(data) }, token),
};