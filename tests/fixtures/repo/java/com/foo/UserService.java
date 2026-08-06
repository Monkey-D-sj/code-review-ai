package com.foo;

public class UserService extends BaseService implements Auth {
    public String name;

    public UserService(String n) {
        this.name = n;
    }

    public boolean authenticate(String user, String pw) {
        return check(user) && BaseService.boot();
    }

    public boolean check(String user) {
        return user.length() > 0;
    }

    public void run() {
    }
}
