export async function loadConstant() {
  return import("./api");
}

export async function loadVariable(path: string) {
  return import(path);
}
