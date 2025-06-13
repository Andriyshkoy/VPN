from bcrypt import gensalt, hashpw


def generate_password_hash(password: str) -> str:
    """
    Generate a hashed password using bcrypt.

    :param password: The plaintext password to hash.
    :return: The hashed password as a string.
    """
    if not password:
        raise ValueError("Password cannot be empty")

    # Generate a salt and hash the password
    salt = gensalt()
    hashed_password = hashpw(password.encode('utf-8'), salt)

    return hashed_password.decode('utf-8')  # Return as string for storage


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python password_generator.py <password>")
        sys.exit(1)

    password = sys.argv[1]
    hashed_password = generate_password_hash(password)
    print(f"Hashed Password: {hashed_password}")
