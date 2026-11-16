variable "TASK_RUNTIME_CONTEXT" {
  default = "../task_runtime"
}

variable "IMAGE_NAME" {
  default = "cityjson-gltf:local"
}

group "default" {
  targets = ["cityjson-gltf"]
}

target "cityjson-gltf" {
  context    = "."
  dockerfile = "Dockerfile"
  tags       = [IMAGE_NAME]
  contexts = {
    task_runtime = TASK_RUNTIME_CONTEXT
  }
}
