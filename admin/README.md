# VPN Admin API

This is an administrative API for managing VPN servers, configurations, users, and billing.

## Authentication

Authenticate by obtaining a token from `/login` and sending it in the
`Authorization` header:

```
Authorization: Bearer your_token
```

## API Endpoints

All list endpoints support `limit` and `offset` query parameters for pagination.

### Server Management

#### List all servers
- **GET** `/servers`
- **Query Parameters**:
  - `limit` (int, optional): Maximum objects per request.
  - `offset` (int, optional): Objects to skip from the beginning.
  - `host` (string, optional): Filter by host.
  - `location` (string, optional): Filter by location.
- **Response**: Array of server objects

#### Create a server
- **POST** `/servers`
- **Body**:
  ```json
  {
    "name": "server1",
    "ip": "192.168.1.1",
    "port": 22,
    "host": "hostname",
    "location": "US",
    "api_key": "server_api_key",
    "cost": 0
  }
  ```
- **Response**: Created server object

#### Get server details
- **GET** `/servers/{server_id}`
- **Response**: Server object

#### Update a server
- **PUT** `/servers/{server_id}`
- **Body**: Any of the server fields that need updating
  ```json
  {
    "name": "updated-name",
    "location": "CA"
  }
  ```
- **Response**: Updated server object

#### Delete a server
- **DELETE** `/servers/{server_id}`
- **Response**: `{"deleted": true}` or `{"deleted": false}`

### Config Management

#### List all configs
- **GET** `/configs`
- **Query Parameters**:
  - `limit` (int, optional): Maximum objects per request.
  - `offset` (int, optional): Objects to skip from the beginning.
  - `server_id` (int, optional): Filter by server ID.
  - `owner_id` (int, optional): Filter by owner ID.
  - `suspended` (bool, optional): Filter by suspension status.
- **Response**: Array of config objects

#### Create a config
- **POST** `/configs`
- **Body**:
  ```json
  {
    "server_id": 1,
    "owner_id": 1,
    "name": "config1",
    "display_name": "User Config 1",
    "use_password": false
  }
  ```
- **Response**: Created config object

#### Download a config
- **GET** `/configs/{config_id}/download`
- **Response**: `.ovpn` file download

#### Delete a config
- **DELETE** `/configs/{config_id}`
- **Response**: `{"deleted": true}` or `{"deleted": false}`

### User Management

#### List all users
- **GET** `/users`
- **Query Parameters**:
  - `limit` (int, optional): Maximum objects per request.
  - `offset` (int, optional): Objects to skip from the beginning.
  - `username` (string, optional): Filter by username.
  - `tg_id` (int, optional): Filter by Telegram ID.
- **Response**: Array of user objects

#### Create a user
- **POST** `/users`
- **Body**:
  ```json
  {
    "tg_id": 123456789,
    "username": "newuser",
    "balance": 0.0
  }
  ```
- **Response**: Created user object

#### View user details with configs
- **GET** `/users/{user_id}`
- **Response**: User object with associated configs
  ```json
  {
    "user": { /* user details */ },
    "configs": [ /* user configs */ ]
  }
  ```

#### Top up user balance
- **POST** `/users/{user_id}/topup`
- **Body**:
  ```json
  {
    "amount": 100.00
  }
  ```
- **Response**: Updated user object

#### Update a user
- **PUT** `/users/{user_id}`
- **Body**: Any of the user fields that need updating
  ```json
  {
    "username": "updated_username",
    "balance": 150.0
  }
  ```
- **Response**: Updated user object

#### Delete a user
- **DELETE** `/users/{user_id}`
- **Response**: `{"deleted": true}` or `{"deleted": false}`

## Examples

### Creating a new server

```bash
curl -X POST http://your-api-domain/servers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "us-east", "ip": "10.0.0.1", "host": "host1.example.com", "location": "US East", "api_key": "server_key123"}'
```

### Creating a new config for a user

```bash
curl -X POST http://your-api-domain/configs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"server_id": 1, "owner_id": 5, "name": "mobile-config", "display_name": "Mobile Device"}'
```

### Adding funds to a user account

```bash
curl -X POST http://your-api-domain/users/5/topup \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount": 50.00}'
```
