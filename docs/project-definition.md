# Project Definition

## Problem Statement

There are multiple servers in a network providing different services. A client may need a particular service without knowing which server currently provides it or whether that server is still available.

Our system allows clients to dynamically discover available services and the network locations of the servers providing them, without requiring the client to know those servers beforehand.


## Service Registry Location

The Service Registry runs as a dedicated network service on a known host.

Clients and service providers are configured with the Registry's IP address and port.

Clients send DISCOVER requests to the Registry, while service providers send registration and availability messages to the Registry.




## System Scope

Our project will implement one Service Discovery System within a single controlled network/domain.

The system will contain multiple clients and service providers, with the Service Registry acting as the central discovery infrastructure for the network.

The Registry will support multiple service types and multiple providers for the same service.

The project does not aim to solve how a device discovers service-discovery infrastructure across the Internet or across different administrative domains. That infrastructure is considered to be part of the network environment.



## Supported Service Types

The Service Discovery System will support multiple service types.

For the initial implementation and demonstration, the system will include:

- FILE — file transfer service
- CHAT — messaging service
- VIDEO — video/streaming service

These services are simplified implementations intended to demonstrate service discovery and network communication. The project does not attempt to recreate full-scale applications such as WhatsApp or other production services.

The same Service Discovery mechanism is used regardless of the service type.

The transport protocol used for actual service communication may differ between services depending on their requirements.





## Provider Registration

Each service provider has a unique Provider ID within our system.

The Provider ID identifies a logical service-provider instance, not necessarily a physical machine.

A single provider can offer multiple services, with each service identified by its service type, transport protocol, and network endpoint.

The transport protocol specifies how the client should communicate with the actual service after discovery.

For example:

Provider-A | FILE    | TCP | 10.0.0.5 | 5001
Provider-A | VIDEO   | UDP | 10.0.0.5 | 5002
Provider-A | CHAT | TCP | 10.0.0.5 | 5003







## Service Discovery Flow

A client discovers a service by sending a DISCOVER request to the Service Registry.

The client specifies the type of service it requires. The Registry searches its current provider records and returns the available providers offering that service.

For example:

DISCOVER | FILE

The Registry may return:

Provider-A | TCP | 10.0.0.5 | 5001
Provider-B | TCP | 10.0.0.6 | 5001

For the basic implementation, the client selects the first available provider returned by the Registry.

The client then communicates directly with the selected service provider. The Registry is used for discovery and is not involved in the actual service communication.

The overall flow is:

Provider → REGISTER → Registry
Client → DISCOVER → Registry
Client ← Provider information ← Registry
Client → Service Provider



## Service Unavailability

If a client requests a service for which no active provider is currently
registered, the Service Registry returns a service-unavailable response.

For example:

DISCOVER | VIDEO

If no active VIDEO provider is available:

ERROR | SERVICE_UNAVAILABLE | VIDEO

The client can then handle the unavailable service appropriately.



## Basic Communication Protocol

The system uses UDP for communication between clients, service providers, and the Service Registry for service discovery and registration.

The transport protocol used for actual service communication depends on the service being provided.

### Provider Registration

A provider registers a service with the Registry using:

REGISTER | Provider ID | Service | Protocol | Port

Example:

REGISTER | Provider-A | FILE | TCP | 5001

The Registry obtains the provider's IP address from the incoming UDP packet and records the provider as active.

The Registry confirms a successful registration using:

REGISTER_OK | Provider ID | Service

Example:

REGISTER_OK | Provider-A | FILE

### Service Discovery

A client requests a service using:

DISCOVER | Service

Example:

DISCOVER | FILE

The Registry searches its active provider records and returns all available providers for the requested service in a single response.

Example:

DISCOVER_RESPONSE | FILE | Provider-A,TCP,10.0.0.5,5001 | Provider-B,TCP,10.0.0.6,5001

For the basic implementation, the client selects the first available provider returned by the Registry.

If no active provider is available for the requested service, the Registry returns:

ERROR | SERVICE_UNAVAILABLE | Service

Example:

ERROR | SERVICE_UNAVAILABLE | FILE



## Actual Service Communication

After discovering a suitable provider, the client communicates directly with the selected service provider.
The Service Registry is not involved in the actual transfer of service data.
The transport protocol used for the service communication is determined by the service.

For example:

FILE service:

Client → Provider using TCP

VIDEO service:

Client → Provider using UDP or another appropriate transport protocol

The service discovery protocol remains UDP regardless of the transport protocol used by the actual service.
