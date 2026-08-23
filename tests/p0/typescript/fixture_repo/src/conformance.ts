export function nestedTarget(): number {
  return 1;
}

export function nestedCaller(): number {
  function localTarget(): number {
    return 1;
  }
  function localCaller(): number {
    return localTarget();
  }
  return localCaller() + nestedTarget();
}

export function mutualA(): number {
  return mutualB();
}

export function mutualB(): number {
  return mutualA();
}

export class OptionalWorker {
  run(): number {
    return 1;
  }
}

export function optionalCall(worker: OptionalWorker): number {
  return worker?.run() ?? 0;
}

export function computedCall(worker: OptionalWorker): number {
  return worker["run"]();
}

export function fieldTarget(): number {
  return 1;
}

export class FieldOwner {
  field = () => fieldTarget();
}
