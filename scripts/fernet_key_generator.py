from cryptography.fernet import Fernet


def generate_fernet_key():
    """Generate a new Fernet key for encryption/decryption."""
    key = Fernet.generate_key()
    return key


def main():
    key = generate_fernet_key()
    print(f"Generated Fernet key: {key.decode()}")


if __name__ == "__main__":
    main()
