package com.acme.p0.calls;

public class Advanced {
    public void lambdaCaller() {
        Runnable task = () -> lambdaTarget();
        task.run();
    }

    public void lambdaTarget() {}

    public void anonymousCaller() {
        new Runnable() {
            @Override
            public void run() {
                anonymousTarget();
            }
        }.run();
    }

    public void anonymousTarget() {}

    public void methodReferenceCaller() {
        Runnable task = this::methodReferenceTarget;
    }

    public void methodReferenceTarget() {}

    public void localClassCaller() {
        class Local {
            void target() {}
        }
        Local local = new Local();
        local.target();
    }
}

abstract class AbstractBase {
    abstract void execute();
}

class AbstractCaller {
    void callAbstract(AbstractBase base) {
        base.execute();
    }
}

class GenericBox<T> {
    void consume(T value) {
    }
}

class GenericCaller {
    void callGeneric(GenericBox<String> box) {
        box.consume("value");
    }
}
