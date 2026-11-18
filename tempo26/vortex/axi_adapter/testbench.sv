`timescale 1ns / 1ps
`default_nettype none

module VX_axi_adapter_tb;
    localparam integer DATAW = 32;
    localparam integer IN_ADDRW = 30;
    localparam integer AXI_ADDRW = 32;
    localparam integer TAGW = 8;
    localparam integer AXIIDW = 4;
    localparam integer STRBW = 4;

    logic clk;
    logic reset;
    logic req_valid;
    logic req_ready;
    logic req_write;
    logic [IN_ADDRW-1:0] req_addr;
    logic [DATAW-1:0] req_wdata;
    logic [STRBW-1:0] req_strb;
    logic [TAGW-1:0] req_tag;
    logic rsp_valid;
    logic rsp_ready;
    logic [DATAW-1:0] rsp_rdata;
    logic [TAGW-1:0] rsp_tag;
    logic rsp_error;
    logic awvalid;
    logic awready;
    logic [AXI_ADDRW-1:0] awaddr;
    logic [AXIIDW-1:0] awid;
    logic [7:0] awlen;
    logic [2:0] awsize;
    logic [1:0] awburst;
    logic [3:0] awcache;
    logic [2:0] awprot;
    logic wvalid;
    logic wready;
    logic [DATAW-1:0] wdata;
    logic [STRBW-1:0] wstrb;
    logic wlast;
    logic bvalid;
    logic bready;
    logic [AXIIDW-1:0] bid;
    logic [1:0] bresp;
    logic arvalid;
    logic arready;
    logic [AXI_ADDRW-1:0] araddr;
    logic [AXIIDW-1:0] arid;
    logic [7:0] arlen;
    logic [2:0] arsize;
    logic [1:0] arburst;
    logic [3:0] arcache;
    logic [2:0] arprot;
    logic rvalid;
    logic rready;
    logic [DATAW-1:0] rdata;
    logic [AXIIDW-1:0] rid;
    logic [1:0] rresp;
    logic rlast;
    logic [31:0] perf_reads;
    logic [31:0] perf_writes;
    logic [31:0] perf_req_stalls;
    logic [31:0] perf_rsp_stalls;
    logic [31:0] perf_axi_errors;
    integer errors;
    integer checks;

    VX_axi_adapter dut (.*);

    always #5 clk = ~clk;

    task automatic check(input logic condition, input string message);
        begin
            checks = checks + 1;
            if (!condition) begin
                errors = errors + 1;
                $display("ERROR %0t: %s", $time, message);
            end
        end
    endtask

    task automatic issue_request(
        input logic write_op,
        input logic [IN_ADDRW-1:0] word_addr,
        input logic [DATAW-1:0] write_data,
        input logic [STRBW-1:0] byte_strobe,
        input logic [TAGW-1:0] tag_value
    );
        begin
            @(negedge clk);
            req_valid = 1'b1;
            req_write = write_op;
            req_addr  = word_addr;
            req_wdata = write_data;
            req_strb  = byte_strobe;
            req_tag   = tag_value;
            #1;
            check(req_ready, "native request was not accepted from idle");
            @(posedge clk);
            @(negedge clk);
            req_valid = 1'b0;
        end
    endtask

    task automatic accept_write_response(
        input logic [AXIIDW-1:0] response_id,
        input logic [1:0] response_code,
        input logic [TAGW-1:0] expected_tag,
        input logic expected_error
    );
        begin
            @(negedge clk);
            bvalid = 1'b1;
            bid    = response_id;
            bresp  = response_code;
            #1;
            check(bready, "write response channel was unexpectedly stalled");
            @(posedge clk);
            #1;
            check(rsp_valid,
                  "native write response must be visible on capture cycle");
            check(rsp_tag == expected_tag, "write response tag mismatch");
            check(rsp_rdata == 0, "write response data must be zero");
            check(rsp_error == expected_error,
                  "write response error flag mismatch");
            @(negedge clk);
            bvalid = 1'b0;
            @(posedge clk);
            #1;
            check(!rsp_valid, "accepted write response did not retire");
        end
    endtask

    task automatic accept_read_response(
        input logic [DATAW-1:0] response_data,
        input logic [AXIIDW-1:0] response_id,
        input logic [1:0] response_code,
        input logic response_last,
        input logic [TAGW-1:0] expected_tag,
        input logic expected_error
    );
        begin
            @(negedge clk);
            rvalid = 1'b1;
            rdata  = response_data;
            rid    = response_id;
            rresp  = response_code;
            rlast  = response_last;
            #1;
            check(rready, "read response channel was unexpectedly stalled");
            @(posedge clk);
            #1;
            check(rsp_valid,
                  "native read response must be visible on capture cycle");
            check(rsp_rdata == response_data, "read response data mismatch");
            check(rsp_tag == expected_tag, "read response tag mismatch");
            check(rsp_error == expected_error,
                  "read response error flag mismatch");
            @(negedge clk);
            rvalid = 1'b0;
            @(posedge clk);
            #1;
            check(!rsp_valid, "accepted read response did not retire");
        end
    endtask

    task automatic test_independent_write_channels;
        begin
            awready = 1'b0;
            wready  = 1'b0;
            issue_request(1'b1, 30'h0000_0123,
                          32'hdead_beef, 4'b1011, 8'ha5);
            #1;
            check(awvalid && wvalid,
                  "write must launch AW and W independently");
            check(awaddr == 32'h0000_048c,
                  "word-to-byte AW address conversion mismatch");
            check(awid == 4'h5, "AW ID must contain low transaction tag bits");
            check(wdata == 32'hdead_beef && wstrb == 4'b1011 && wlast,
                  "write data payload mismatch");
            check(awlen == 0 && awsize == 3'd2 && awburst == 2'b01,
                  "AW single-beat attributes mismatch");

            awready = 1'b1;
            @(posedge clk);
            #1;
            check(!awvalid && wvalid,
                  "AW must drop after handshake while W remains pending");
            check(wdata == 32'hdead_beef && wstrb == 4'b1011,
                  "W payload changed while waiting independently");
            awready = 1'b0;
            repeat (2) begin
                @(posedge clk);
                #1;
                check(wvalid, "W valid dropped during data-channel stall");
            end
            wready = 1'b1;
            @(posedge clk);
            #1;
            check(!wvalid, "W valid did not drop after its handshake");
            wready = 1'b0;
            accept_write_response(4'h5, 2'b00, 8'ha5, 1'b0);
        end
    endtask

    task automatic test_read_and_native_stall;
        logic [31:0] stalls_before;
        begin
            arready = 1'b0;
            issue_request(1'b0, 30'h0000_0040, 0, 0, 8'h3c);
            #1;
            check(arvalid, "read address request missing");
            check(araddr == 32'h0000_0100 && arid == 4'hc,
                  "read address or ID conversion mismatch");
            check(arlen == 0 && arsize == 3'd2 && arburst == 2'b01,
                  "AR single-beat attributes mismatch");
            stalls_before = perf_req_stalls;
            @(negedge clk);
            req_valid = 1'b1;
            req_write = 1'b1;
            #1;
            check(!req_ready, "busy adapter accepted a second request");
            repeat (2) @(posedge clk);
            #1;
            check(perf_req_stalls >= stalls_before + 2,
                  "native request stalls were not counted");
            @(negedge clk);
            req_valid = 1'b0;
            arready = 1'b1;
            @(posedge clk);
            #1;
            check(!arvalid, "AR valid did not retire after handshake");
            arready = 1'b0;
            accept_read_response(32'hc001_cafe, 4'hc, 2'b00,
                                 1'b1, 8'h3c, 1'b0);
        end
    endtask

    task automatic test_response_backpressure;
        logic [31:0] stalls_before;
        begin
            rsp_ready = 1'b0;
            awready = 1'b1;
            wready  = 1'b1;
            issue_request(1'b1, 30'h20, 32'h5566_7788, 4'hf, 8'h72);
            @(posedge clk);
            #1;
            awready = 1'b0;
            wready  = 1'b0;
            @(negedge clk);
            bvalid = 1'b1;
            bid = 4'h2;
            bresp = 2'b00;
            @(posedge clk);
            #1;
            check(rsp_valid && rsp_tag == 8'h72,
                  "backpressured response was not captured");
            @(negedge clk);
            bvalid = 1'b0;
            stalls_before = perf_rsp_stalls;
            repeat (3) begin
                @(posedge clk);
                #1;
                check(rsp_valid, "native response valid dropped under stall");
                check(rsp_tag == 8'h72 && rsp_rdata == 0,
                      "native response payload changed under stall");
            end
            check(perf_rsp_stalls >= stalls_before + 3,
                  "native response stalls were not counted");
            @(negedge clk);
            rsp_ready = 1'b1;
            @(posedge clk);
            #1;
            check(!rsp_valid, "stalled response failed to retire");
        end
    endtask

    task automatic test_protocol_errors;
        begin
            arready = 1'b1;
            issue_request(1'b0, 30'h55, 0, 0, 8'h19);
            @(posedge clk);
            #1;
            arready = 1'b0;
            accept_read_response(32'hbad0_f00d, 4'h8, 2'b10,
                                 1'b0, 8'h19, 1'b1);
            check(perf_axi_errors == 1,
                  "read protocol error counter mismatch");

            awready = 1'b1;
            wready = 1'b1;
            issue_request(1'b1, 30'h66, 32'h1234_5678, 4'hf, 8'h2e);
            @(posedge clk);
            #1;
            awready = 1'b0;
            wready = 1'b0;
            accept_write_response(4'hf, 2'b00, 8'h2e, 1'b1);
            check(perf_axi_errors == 2,
                  "write ID error counter mismatch");
        end
    endtask

    initial begin
        clk = 1'b0;
        reset = 1'b1;
        req_valid = 1'b0;
        req_write = 1'b0;
        req_addr = '0;
        req_wdata = '0;
        req_strb = '0;
        req_tag = '0;
        rsp_ready = 1'b1;
        awready = 1'b0;
        wready = 1'b0;
        bvalid = 1'b0;
        bid = '0;
        bresp = '0;
        arready = 1'b0;
        rvalid = 1'b0;
        rdata = '0;
        rid = '0;
        rresp = '0;
        rlast = 1'b1;
        errors = 0;
        checks = 0;

        repeat (4) @(posedge clk);
        @(negedge clk);
        reset = 1'b0;
        #1;
        check(req_ready, "adapter must be ready after reset");
        check(!rsp_valid && !awvalid && !wvalid && !arvalid && !bready && !rready,
              "AXI/native valid state was not idle after reset");
        check(perf_reads == 0 && perf_writes == 0
              && perf_req_stalls == 0 && perf_rsp_stalls == 0
              && perf_axi_errors == 0,
              "reset state or counters are incorrect");

        test_independent_write_channels();
        test_read_and_native_stall();
        test_response_backpressure();
        test_protocol_errors();

        check(perf_reads == 2, "accepted read count mismatch");
        check(perf_writes == 3, "accepted write count mismatch");
        if (errors == 0) begin
            $display("PASS: VX_axi_adapter completed %0d checks", checks);
            $finish;
        end else begin
            $fatal(1, "VX_axi_adapter failed %0d of %0d checks", errors, checks);
        end
    end

    initial begin
        #20000;
        $fatal(1, "VX_axi_adapter timeout");
    end
endmodule

`default_nettype wire
