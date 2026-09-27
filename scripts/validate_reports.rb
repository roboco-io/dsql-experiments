# frozen_string_literal: true

require "date"
require "yaml"

root = File.expand_path("..", __dir__)
files = Dir.glob(File.join(root, "docs/_experiments/*.md")).sort
abort "No experiment reports found" if files.empty?

errors = []
identifiers = []
files.each do |file|
  name = File.basename(file)
  source = File.read(file)
  front_matter = source.match(/\A---\r?\n(.*?)\r?\n---\r?\n/m)
  unless front_matter
    errors << "#{name}: missing YAML front matter"
    next
  end
  begin
    data = YAML.safe_load(front_matter[1], permitted_classes: [Date])
  rescue Psych::Exception => e
    errors << "#{name}: #{e.message}"
    next
  end
  unless data.is_a?(Hash)
    errors << "#{name}: metadata must be a mapping"
    next
  end
  %w[experiment_id slug title question priority status issue_url].each do |key|
    errors << "#{name}: missing #{key}" unless data[key].is_a?(String) && !data[key].strip.empty?
  end
  identifier = data["experiment_id"].to_s
  identifiers << identifier
  errors << "#{name}: invalid experiment_id" unless identifier.match?(/\AE\d{3}\z/)
  errors << "#{name}: slug must match file name and experiment ID" unless data["slug"] == File.basename(file, ".md") && data["slug"] == identifier.downcase
  errors << "#{name}: invalid status" unless %w[planned running completed].include?(data["status"])
  errors << "#{name}: invalid priority" unless %w[P0 P1 P2].include?(data["priority"])
  issue = data["issue"]
  expected_url = "https://github.com/roboco-io/rds-experiments/issues/#{issue}"
  errors << "#{name}: invalid issue link" unless issue.is_a?(Integer) && issue.positive? && data["issue_url"] == expected_url

  next unless data["status"] == "completed"

  %w[summary result measured_at code_commit].each do |key|
    errors << "#{name}: completed reports require #{key}" if data[key].to_s.strip.empty?
  end
  begin
    Date.iso8601(data["measured_at"].to_s)
  rescue Date::Error
    errors << "#{name}: measured_at must be an ISO date"
  end
  errors << "#{name}: code_commit must be a full Git SHA" unless data["code_commit"].to_s.match?(/\A[0-9a-f]{40}\z/)
  runs = data["run_ids"]
  errors << "#{name}: completed reports require run_ids" unless runs.is_a?(Array) && !runs.empty? && runs.all? { |run| run.is_a?(String) && !run.strip.empty? }
  errors << "#{name}: cleanup must be verified with zero remaining resources" unless data["cleanup_verified"] == true && data["remaining_resources"] == 0
  body = source[front_matter.end(0)..]
  ["실행 조건", "성능 결과", "개발·운영 편의성", "비용", "결론과 한계", "정리 기록"].each do |heading|
    errors << "#{name}: missing result section #{heading}" unless body.match?(/^## #{Regexp.escape(heading)}\s*$/)
  end
  errors << "#{name}: remove the unmeasured placeholder before completing" if body.include?("아직 실험을 실행하지 않았습니다")
end
errors << "Experiment IDs must be unique" unless identifiers.uniq == identifiers
abort errors.join("\n") unless errors.empty?
puts "Validated #{files.length} experiment reports"
