packer {
  required_plugins {
    googlecompute = {
      version = ">= 1.1.1"
      source  = "github.com/hashicorp/googlecompute"
    }
  }
}

variable "project_id" {
  type    = string
  default = env("GCP_PROJECT_ID")
}

variable "zone" {
  type    = string
  default = "us-central1-f"
}

source "googlecompute" "agent_image" {
  project_id          = var.project_id
  source_image_family = "ubuntu-2204-lts"
  zone                = var.zone
  image_name          = "blueprint-agent-v1-{{timestamp}}"
  image_family        = "blueprint-agent"
  ssh_username        = "packer"
}

build {
  sources = ["source.googlecompute.agent_image"]

  provisioner "shell" {
    inline = [
      "sudo apt-get update",
      "sudo apt-get install -y curl git wget gnupg",
      "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -",
      "sudo apt-get install -y nodejs",
      "sudo npm install -g @anthropic-ai/claude-code",
      "git config --global user.email 'blueprint-agent@automated.dev'",
      "git config --global user.name 'Blueprint Agent'",
      "sudo mkdir -p /opt/agent",
      "sudo chown -R packer:packer /opt/agent"
    ]
  }

  provisioner "file" {
    source      = "../../agent/src"
    destination = "/opt/agent/src"
  }

  provisioner "file" {
    source      = "../../agent/package.json"
    destination = "/opt/agent/package.json"
  }

  provisioner "file" {
    source      = "../../agent/package-lock.json"
    destination = "/opt/agent/package-lock.json"
  }

  provisioner "shell" {
    inline = [
      "sudo useradd -m -s /bin/bash agent",
      "cd /opt/agent && npm ci",
      "sudo chown -R agent:agent /opt/agent",
      "sudo touch /etc/agent.env",
      "sudo chmod 644 /etc/agent.env",
      "echo '[Unit]' | sudo tee /etc/systemd/system/blueprint-agent.service",
      "echo 'Description=Blueprint Agent Service' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'After=network.target' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '[Service]' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'Type=simple' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'User=agent' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'WorkingDirectory=/opt/agent' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'EnvironmentFile=/etc/agent.env' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'ExecStart=/usr/bin/npm start' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'Restart=no' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'MemoryMax=6G' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'TimeoutStopSec=30' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo '[Install]' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "echo 'WantedBy=multi-user.target' | sudo tee -a /etc/systemd/system/blueprint-agent.service",
      "sudo systemctl daemon-reload"
    ]
  }
}
