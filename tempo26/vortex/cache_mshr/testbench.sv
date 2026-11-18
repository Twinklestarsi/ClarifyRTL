`timescale 1ns/1ps
`default_nettype none
module VX_cache_mshr_tb;
    localparam ADDRW = 16;
    localparam DATAW = 32;
    localparam MSHR_SIZE = 8;
    localparam IDW = 3;
    localparam COUNTW = 16;

    logic clk = 0;
    logic reset = 1;
    logic flush = 0;
    logic allocate_valid = 0;
    logic [ADDRW-1:0] allocate_addr = 0;
    logic allocate_write = 0;
    logic [DATAW-1:0] allocate_data = 0;
    logic [3:0] allocate_mask = 0;
    logic [7:0] allocate_tag = 0;
    wire allocate_ready;
    wire [IDW-1:0] allocate_id;
    wire allocate_pending;
    wire [IDW-1:0] allocate_previd;
    logic finalize_valid = 0;
    logic finalize_release = 0;
    logic finalize_pending = 0;
    logic [IDW-1:0] finalize_previd = 0;
    logic [IDW-1:0] finalize_id = 0;
    logic fill_valid = 0;
    logic [IDW-1:0] fill_id = 0;
    wire [ADDRW-1:0] fill_addr;
    wire dequeue_valid;
    logic dequeue_ready = 0;
    wire [ADDRW-1:0] dequeue_addr;
    wire dequeue_write;
    wire [DATAW-1:0] dequeue_data;
    wire [3:0] dequeue_mask;
    wire [7:0] dequeue_tag;
    wire [IDW-1:0] dequeue_id;
    wire [MSHR_SIZE-1:0] valid_entries;
    wire [IDW:0] occupancy;
    wire full;
    wire [COUNTW-1:0] allocation_count;
    wire [COUNTW-1:0] replay_count;
    wire [COUNTW-1:0] merge_count;

    integer errors = 0;
    integer checks = 0;
    always #5 clk = ~clk;

    VX_cache_mshr #(
        .ADDRW(ADDRW),
        .DATAW(DATAW),
        .MSHR_SIZE(MSHR_SIZE),
        .COUNTW(COUNTW)
    ) dut (.*);

    task automatic check(input logic condition, input string message);
        begin
            checks = checks + 1;
            if (!condition) begin
                errors = errors + 1;
                $display("ERROR %0t: %s", $time, message);
            end
        end
    endtask

    task automatic stop_allocate;
        begin
            allocate_valid = 0;
            allocate_addr = 0;
            allocate_write = 0;
            allocate_data = 0;
            allocate_mask = 0;
            allocate_tag = 0;
        end
    endtask

    initial begin
        repeat (2) @(posedge clk);
        @(negedge clk);
        reset = 0;
        check(allocate_ready, "empty MSHR should accept allocation");
        check(occupancy == 0, "reset occupancy");
        check(!full && valid_entries == '0 && !dequeue_valid,
              "reset structural state");
        check(allocation_count == 0 && replay_count == 0 && merge_count == 0,
              "reset event counters");

        // Allocate the head of address A.
        allocate_valid = 1;
        allocate_addr = 16'h1200;
        allocate_write = 0;
        allocate_data = 32'h1111_0001;
        allocate_mask = 4'b1111;
        allocate_tag = 8'hA1;
        #1;
        check(allocate_id == 0, "first free id");
        check(!allocate_pending, "first address unexpectedly pending");
        @(posedge clk);
        #1;
        check(valid_entries[0], "head allocation not recorded");
        check(occupancy == 1, "occupancy after head allocation");

        // Allocate a second request for A, then explicitly link it to head.
        @(negedge clk);
        allocate_data = 32'h2222_0002;
        allocate_mask = 4'b0011;
        allocate_tag = 8'hA2;
        #1;
        check(allocate_id == 1, "second free id");
        check(allocate_pending, "matching address was not detected");
        check(allocate_previd == 0, "matching chain tail incorrect");
        @(posedge clk);

        @(negedge clk);
        stop_allocate();
        finalize_valid = 1;
        finalize_pending = 1;
        finalize_previd = 0;
        finalize_id = 1;
        @(posedge clk);

        // A different address allocates independently.
        @(negedge clk);
        finalize_valid = 0;
        finalize_pending = 0;
        allocate_valid = 1;
        allocate_addr = 16'h3400;
        allocate_write = 1;
        allocate_data = 32'h3333_0003;
        allocate_mask = 4'b1100;
        allocate_tag = 8'hB1;
        #1;
        check(!allocate_pending, "different address falsely merged");
        check(allocate_id == 2, "third allocation id");
        @(posedge clk);
        #1;
        check(occupancy == 3, "occupancy after three allocations");
        check(allocation_count == 3, "allocation event count");
        check(merge_count == 1, "merge event count");

        // Hold replay output and verify the fill starts without an extra cycle.
        @(negedge clk);
        stop_allocate();
        fill_id = 0;
        fill_valid = 1;
        dequeue_ready = 0;
        #1;
        check(fill_addr == 16'h1200, "fill address lookup");
        @(posedge clk);
        #1;
        check(dequeue_valid, "fill did not start replay immediately");
        check(dequeue_id == 0, "replay head id");
        check(dequeue_addr == 16'h1200, "replay head address");
        check(dequeue_data == 32'h1111_0001, "replay head data");
        check(dequeue_mask == 4'b1111, "replay head byte mask");
        check(dequeue_tag == 8'hA1, "replay head tag");
        check(!dequeue_write, "replay head operation");

        // Backpressure must keep the head stable.
        @(negedge clk);
        fill_valid = 0;
        repeat (2) begin
            @(posedge clk);
            #1;
            check(dequeue_valid && dequeue_id == 0, "replay changed while stalled");
            check(dequeue_data == 32'h1111_0001, "stalled replay data changed");
        end

        // Consuming head follows the link to the merged entry without a gap.
        @(negedge clk);
        dequeue_ready = 1;
        @(posedge clk);
        #1;
        check(dequeue_valid, "linked replay inserted a bubble");
        check(dequeue_id == 1, "linked replay id");
        check(dequeue_data == 32'h2222_0002, "linked replay data");
        check(dequeue_mask == 4'b0011, "linked replay mask");
        check(dequeue_tag == 8'hA2, "linked replay tag");
        check(occupancy == 2, "head was not released");

        @(posedge clk);
        #1;
        check(!dequeue_valid, "replay did not end at chain tail");
        check(occupancy == 1, "chain tail was not released");
        check(replay_count == 2, "replay event count");

        // Finalize-release handles a hit or cancelled miss.
        @(negedge clk);
        dequeue_ready = 0;
        finalize_valid = 1;
        finalize_release = 1;
        finalize_id = 2;
        @(posedge clk);
        #1;
        check(occupancy == 0, "finalize release did not free entry");
        check(valid_entries == 0, "valid table not empty");

        // Flush also cancels an active replay and clears every entry.
        @(negedge clk);
        finalize_valid = 0;
        finalize_release = 0;
        allocate_valid = 1;
        allocate_addr = 16'h5500;
        @(posedge clk);
        @(negedge clk);
        stop_allocate();
        flush = 1;
        @(posedge clk);
        #1;
        check(occupancy == 0 && !dequeue_valid, "flush state cleanup");

        if (errors != 0) begin
            $fatal(1, "VX_cache_mshr: %0d errors from %0d checks", errors, checks);
        end
        $display("PASS VX_cache_mshr: %0d checks", checks);
        $finish;
    end
endmodule

`default_nettype wire
