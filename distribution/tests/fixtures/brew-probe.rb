# A bounded Formula DSL probe, never a substitute for native Homebrew checks.
require "json"
module OS
  def self.mac?; ENV.fetch("PROBE_OS") == "darwin"; end
  def self.linux?; ENV.fetch("PROBE_OS") == "linux"; end
end
module Hardware
  module CPU
    def self.arm?; ENV.fetch("PROBE_CPU") == "arm64"; end
    def self.intel?; ["amd64", "x86"].include?(ENV.fetch("PROBE_CPU")); end
    def self.is_64_bit?; ["amd64", "arm64"].include?(ENV.fetch("PROBE_CPU")); end
  end
end
class Formula
  def self.inherited(child)
    $formula = child
  end
  def self.desc(value); end
  def self.homepage(value); end
  def self.version(value); end
  def self.license(value); end
  def self.on_macos(&block)
    class_eval(&block) if OS.mac?
  end
  def self.on_linux(&block)
    class_eval(&block) if OS.linux?
  end
  def self.on_arm(&block)
    class_eval(&block) if Hardware::CPU.arm?
  end
  def self.on_intel(&block)
    class_eval(&block) if Hardware::CPU.intel?
  end
  def self.url(value, using:)
    $selection = {"url" => value, "using" => using}
  end
  def self.sha256(value); $selection["sha256"] = value; end
  def self.test(&block); end
  class Bin
    def install(mapping)
      $selection ||= {}
      $selection["bin"] = mapping
    end
  end
  def bin; Bin.new; end
  def odie(message); raise(message); end
end
load ARGV.fetch(0)
begin
  $formula.new.install
  puts JSON.generate($selection)
rescue => e
  warn e.message
  exit 1
end
