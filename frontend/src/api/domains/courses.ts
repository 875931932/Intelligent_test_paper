import { request } from '../http';
import type { CourseCreate, CourseResponse, CourseUpdate } from '../../types/api';

export const coursesApi = {
  list: (token?: string): Promise<CourseResponse[]> =>
    request('/courses', undefined, token),
  create: (data: CourseCreate, token?: string): Promise<CourseResponse> =>
    request('/courses', { method: 'POST', body: JSON.stringify(data) }, token),
  get: (courseId: string, token?: string): Promise<CourseResponse> =>
    request('/courses/' + courseId, undefined, token),
  update: (courseId: string, data: CourseUpdate, token?: string): Promise<CourseResponse> =>
    request('/courses/' + courseId, { method: 'PATCH', body: JSON.stringify(data) }, token),
};