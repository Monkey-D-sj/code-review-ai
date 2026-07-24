class UserService:
    def authenticate(self, user, pw) -> bool:
        return check(pw)


def login(user, pw) -> str:
    return user
