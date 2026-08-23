export function tag(strings: TemplateStringsArray): string {
  return strings[0] || "";
}

export function taggedCall(): string {
  return tag`hello`;
}

export function taggedBoundary(): string {
  return missingTag`hello`;
}

export class AccessorHolder {
  private _value = "value";

  get value(): string {
    return this._value;
  }

  set value(next: string) {
    this._value = next;
  }
}

export function accessorRead(holder: AccessorHolder): string {
  return holder.value;
}
