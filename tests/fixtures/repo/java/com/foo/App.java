package com.foo;

import com.foo.UserService;
import com.foo.PasswordChecker;
import static com.foo.util.Util.compute;

public class App {
    public static void main(String[] args) {
        UserService svc = new UserService("n");
        PasswordChecker.check("u");
        svc.authenticate("u", "p");
        compute();
    }
}
