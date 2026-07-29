export class UserService {
  authenticate(user: string, pw: string): boolean {
    return check(pw);
  }
}

export function login(user: string, pw: string): string {
  return user;
}
