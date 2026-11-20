resource "aws_elasticache_cluster" "sessions" {
  cluster_id      = "sessions"
  engine          = "redis"
  node_type       = "cache.t3.micro"
  num_cache_nodes = 1
}
