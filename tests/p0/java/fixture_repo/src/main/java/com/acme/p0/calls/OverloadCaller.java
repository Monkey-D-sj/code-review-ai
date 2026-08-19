package com.acme.p0.calls;

import com.acme.p0.scope.OverloadA;
import com.acme.p0.scope.OverloadB;

public class OverloadCaller {
    public void run() {
        OverloadA a = new OverloadA();
        OverloadB b = new OverloadB();
        a.process();
        b.process("scope");
    }
}
