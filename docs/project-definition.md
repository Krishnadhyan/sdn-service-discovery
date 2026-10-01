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
