# VPN Admin API

This is an administrative API for managing VPN servers, configurations, users, and billing.

## Authentication

All endpoints require authentication using an API key. Include the API key in the request headers:

```
X-API-Key: your_admin_api_key
```

## API Endpoints

### Server Management

#### List all servers
- **GET** `/servers`
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
- **Response**: `{"deleted": true}`

### User Management

#### List all users
- **GET** `/users`
- **Response**: Array of user objects

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

## Examples

### Creating a new server

```bash
curl -X POST http://your-api-domain/servers \
  -H "X-API-Key: your_admin_api_key" \
  -H "Content-Type: application/json" \
  -d '{"name": "us-east", "ip": "10.0.0.1", "host": "host1.example.com", "location": "US East", "api_key": "server_key123"}'
```

### Creating a new config for a user

```bash
curl -X POST http://your-api-domain/configs \
  -H "X-API-Key: your_admin_api_key" \
  -H "Content-Type: application/json" \
  -d '{"server_id": 1, "owner_id": 5, "name": "mobile-config", "display_name": "Mobile Device"}'
```

### Adding funds to a user account

```bash
curl -X POST http://your-api-domain/users/5/topup \
  -H "X-API-Key: your_admin_api_key" \
  -H "Content-Type: application/json" \
  -d '{"amount": 50.00}'
```
