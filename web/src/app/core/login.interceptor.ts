import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { catchError, throwError } from 'rxjs';

/** Behind Caddy, an expired session comes back as 401 LOGIN_REQUIRED: send the browser to the login page. */
export const loginInterceptor: HttpInterceptorFn = (req, next) =>
  next(req).pipe(catchError((e: HttpErrorResponse) => {
    if (e.status === 401 && e.error?.code === 'LOGIN_REQUIRED' && typeof location !== 'undefined') {
      location.assign('/auth/login?next=' + encodeURIComponent(location.pathname + location.search));
    }
    return throwError(() => e);
  }));
