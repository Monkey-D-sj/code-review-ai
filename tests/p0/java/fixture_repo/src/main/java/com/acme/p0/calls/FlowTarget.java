package com.acme.p0.calls;

public class FlowTarget {
    public FlowTarget() {}

    public void bareTarget() {}
    public void thisTarget() {}
    public static void staticTarget() {}
    public void ifTarget() {}
    public void elseTarget() {}
    public void forTarget() {}
    public void whileTarget() {}
    public void tryTarget() {}
    public void catchTarget() {}
    public void finallyTarget() {}

    public class Inner {
        public void innerTarget() {}
    }
}
