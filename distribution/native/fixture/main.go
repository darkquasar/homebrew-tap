// Synthetic executable for fixture-mode native checks only; never published.
// It prints the same shape as Patronus: "patronus version v2.7.1".
package main

import "fmt"

var name, version string

func main() { fmt.Printf("%s version v%s\n", name, version) }
