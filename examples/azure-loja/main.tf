# Fictitious company: an online shop on Microsoft Azure (Netherlands).
terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

resource "azurerm_resource_group" "main" {
  name     = "shop-rg"
  location = "westeurope"
}

# ---------------------------------------------------------------- network
resource "azurerm_virtual_network" "main" {
  name                = "shop-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_subnet" "web" {
  name                 = "web"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_network_security_group" "web" {
  name                = "web-nsg"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_network_interface" "web" {
  name                = "web-nic"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.web.id
    private_ip_address_allocation = "Dynamic"
  }
}

# ---------------------------------------------------------------- compute
resource "azurerm_linux_virtual_machine" "web" {
  name                  = "shop-web"
  resource_group_name   = azurerm_resource_group.main.name
  location              = azurerm_resource_group.main.location
  size                  = "Standard_B2s"
  admin_username        = "shop"
  network_interface_ids = [azurerm_network_interface.web.id]

  admin_ssh_key {
    username   = "shop"
    public_key = "ssh-ed25519 AAAAexamplekey shop@example"
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }
}

# ---------------------------------------------------------------- storage and data
resource "azurerm_storage_account" "assets" {
  name                     = "shopassets001"
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
}

resource "azurerm_storage_container" "images" {
  name                  = "images"
  storage_account_id    = azurerm_storage_account.assets.id
  container_access_type = "private"
}

resource "azurerm_postgresql_flexible_server" "db" {
  name                   = "shop-db"
  resource_group_name    = azurerm_resource_group.main.name
  location               = azurerm_resource_group.main.location
  version                = "16"
  sku_name               = "B_Standard_B1ms"
  administrator_login    = "shop"
  administrator_password = "change-me-not-a-real-password"
  storage_mb             = 32768
}

resource "azurerm_cosmosdb_account" "carts" {
  name                = "shop-carts"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  offer_type          = "Standard"

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.main.location
    failover_priority = 0
  }
}

# ---------------------------------------------------------------- functions
resource "azurerm_service_plan" "functions" {
  name                = "shop-functions-plan"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = "Y1"
}

resource "azurerm_linux_function_app" "orders" {
  name                       = "shop-orders"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  service_plan_id            = azurerm_service_plan.functions.id
  storage_account_name       = azurerm_storage_account.assets.name
  storage_account_access_key = azurerm_storage_account.assets.primary_access_key

  site_config {}
}
