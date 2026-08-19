export function fetchUser(id: string): string {
  return id;
}

export async function fetchUserAsync(id: string): Promise<string> {
  return id;
}

export const parseUser = (value: string): string => value.trim();
