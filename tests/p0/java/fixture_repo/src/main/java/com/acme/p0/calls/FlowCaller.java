package com.acme.p0.calls;

import com.acme.p0.imports.TargetService;
import com.acme.p0.inheritance.*;
import static com.acme.p0.imports.StaticImports.staticSave;

public class FlowCaller {
    public void bareTarget() {}
    public void thisTarget() {}
    public void ifTarget() {}
    public void elseTarget() {}
    public void forTarget() {}
    public void whileTarget() {}
    public void tryTarget() {}
    public void catchTarget() {}
    public void finallyTarget() {}

    public void run() {
        bareTarget();
        this.thisTarget();
        FlowTarget.staticTarget();
        new FlowTarget();
        if (true) {
            ifTarget();
        } else {
            elseTarget();
        }
        for (int i = 0; i < 1; i++) {
            forTarget();
        }
        while (false) {
            whileTarget();
        }
        try {
            tryTarget();
        } catch (RuntimeException ex) {
            catchTarget();
        } finally {
            finallyTarget();
        }
        TargetService.save();
        staticSave();
        com.acme.p0.imports.StaticImports.staticLoad();
    }

    public void innerScope() {
        FlowTarget.Inner inner = new FlowTarget().new Inner();
        inner.innerTarget();
    }
}
